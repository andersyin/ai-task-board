#!/usr/bin/env python3
"""board-lease-check — 检测租约超时任务，自动重排队或进 DLQ。

用法: python3 board-lease-check.py [--board-root <path>] [--dry-run]
退出码: 0=成功; 1=错误

规则:
  - 扫描 tasks/*/status.json
  - 找 status="claimed" 且 lease_expires_at 已过期的任务
  - requeue_count < 2: 改回 open, requeue_count+1, 清除 claimed_at/lease_expires_at/executor/executor_model
  - requeue_count >= 2: 改为 dlq 状态
  - status="blocked" 超过 24h 且无人解除: 改回 open, requeue_count+1（给其他端机会）
  - --dry-run: 只报告不修改
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_contract import is_physical_task_dir, validate_task_id
from board_transition import SYSTEM_REQUEUE_ACTOR, TransitionError, transition_task

# 北京时区
CN_TZ = timezone(timedelta(hours=8))


def parse_iso(ts_str):
    """解析 ISO 8601 时间戳，返回 aware datetime 对象（统一带时区）。"""
    if not ts_str:
        return None
    ts_str = ts_str.strip()
    try:
        import re
        if re.search(r"[+-]\d{4}$", ts_str):
            ts_str = ts_str[:-2] + ":" + ts_str[-2:]
        dt = datetime.fromisoformat(ts_str)
        # 如果解析出来是 naive，假设是北京时间
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    except ValueError:
        return None


def now_cn():
    """当前北京时间（aware datetime）。"""
    return datetime.now(CN_TZ)


def check_leases(board_root, dry_run=False):
    """检查所有任务的租约状态。"""
    tasks_root = board_root / "tasks"
    if not tasks_root.exists():
        print(f"❌ tasks 目录不存在: {tasks_root}", file=sys.stderr)
        return 1

    now = now_cn()
    actions = []
    scan_errors = 0

    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")):
            continue
        if not is_physical_task_dir(task_dir, tasks_root):
            print(f"❌ 跳过 {task_dir.name}: 不是 tasks/ 下的安全真实目录", file=sys.stderr)
            scan_errors += 1
            continue

        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue

        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ 跳过 {task_dir.name}: status.json 解析失败: {e}", file=sys.stderr)
            continue

        task_id = status.get("id", task_dir.name)
        if task_id != task_dir.name or not validate_task_id(task_dir.name):
            print(
                f"❌ 跳过 {task_dir.name}: status.id={task_id!r} 与安全目录名不一致",
                file=sys.stderr,
            )
            scan_errors += 1
            continue
        current_status = status.get("status")
        lease_expires = status.get("lease_expires_at")
        requeue_count = status.get("requeue_count", 0)

        # 检查 claimed 租约超时
        if current_status == "claimed" and lease_expires:
            expiry = parse_iso(lease_expires)
            if not expiry:
                print(f"❌ [{task_id}] lease_expires_at 无法解析: {lease_expires!r}", file=sys.stderr)
                scan_errors += 1
                continue
            if expiry and now > expiry:
                new_requeue = requeue_count + 1
                if new_requeue >= 2:
                    actions.append({
                        "task_id": task_id,
                        "task_dir": task_dir.name,
                        "expected_lease": lease_expires,
                        "action": "dlq",
                        "reason": f"租约超时 (过期于 {lease_expires})，requeue_count {requeue_count}→{new_requeue} ≥ 2，进入死信队列",
                        "old_status": "claimed",
                        "new_status": "dlq",
                        "changes": {
                            "status": "dlq",
                            "requeue_count": new_requeue,
                            "lease_expires_at": None,
                        }
                    })
                else:
                    actions.append({
                        "task_id": task_id,
                        "task_dir": task_dir.name,
                        "expected_lease": lease_expires,
                        "action": "requeue",
                        "reason": f"租约超时 (过期于 {lease_expires})，requeue_count {requeue_count}→{new_requeue}，重新排队",
                        "old_status": "claimed",
                        "new_status": "open",
                        "changes": {
                            "status": "open",
                            "requeue_count": new_requeue,
                            "executor": None,
                            "executor_model": None,
                            "claimed_at": None,
                            "lease_expires_at": None,
                        }
                    })

        # 检查 blocked 超过 24h（给其他端机会）
        if current_status == "blocked":
            blocked_at = status.get("blocked_at")
            if blocked_at:
                blocked_time = parse_iso(blocked_at)
                if not blocked_time:
                    print(f"❌ [{task_id}] blocked_at 无法解析: {blocked_at!r}", file=sys.stderr)
                    scan_errors += 1
                elif now > blocked_time + timedelta(hours=24):
                    new_requeue = requeue_count + 1
                    actions.append({
                        "task_id": task_id,
                        "task_dir": task_dir.name,
                        "expected_blocked_at": blocked_at,
                        "action": "unblock_requeue",
                        "reason": f"阻塞超过 24h (阻塞于 {blocked_at})，自动解除给其他端机会，requeue_count {requeue_count}→{new_requeue}",
                        "old_status": "blocked",
                        "new_status": "open",
                        "changes": {
                            "status": "open",
                            "requeue_count": new_requeue,
                            "executor": None,
                            "executor_model": None,
                            "claimed_at": None,
                            "lease_expires_at": None,
                            "blocked_at": None,
                        }
                    })

    # 执行修改
    if not actions:
        print("✅ 租约检查完成：无需处理的超时任务")
        return 1 if scan_errors else 0

    print(f"📋 租约检查发现 {len(actions)} 个需要处理的任务：\n")
    for a in actions:
        print(f"  [{a['task_id']}] {a['old_status']} → {a['new_status']}")
        print(f"    原因: {a['reason']}")
        print()

    if dry_run:
        print("⚠️ --dry-run 模式，未实际修改文件")
        return 1 if scan_errors else 0

    # 实际修改
    write_errors = 0
    for a in actions:
        task_dir = tasks_root / a["task_dir"]
        status_path = task_dir / "status.json"
        try:
            changes = dict(a["changes"])
            changes["status_changed_at"] = now.isoformat(timespec="seconds")
            expected_fields = {}
            if a.get("expected_lease"):
                expected_fields["lease_expires_at"] = a["expected_lease"]
            if a.get("expected_blocked_at"):
                expected_fields["blocked_at"] = a["expected_blocked_at"]
            discriminator = a.get("expected_lease") or a.get("expected_blocked_at") or "manual"
            transition_task(
                board_root,
                a["task_dir"],
                action="requeue",
                actor=SYSTEM_REQUEUE_ACTOR,
                actor_model="deterministic-v1",
                request_id=f"lease:{a['task_id']}:{a['action']}:{discriminator}",
                updates=changes,
                request_payload={
                    "task": a["task_id"],
                    "action": a["action"],
                    "reason": a["reason"],
                    "target": a["new_status"],
                },
                expected_status=a["old_status"],
                expected_fields=expected_fields,
            )
            print(f"  ✅ 已修改 {a['task_id']}/status.json: {a['old_status']} → {a['new_status']}")
        except TransitionError as e:
            print(f"  ❌ 修改 {a['task_id']} 失败: {e}", file=sys.stderr)
            write_errors += 1

    # 修改后自动刷新索引
    print("\n🔄 刷新 board-index.json...")
    import subprocess
    gen_script = board_root / "board-index-gen.py"
    if gen_script.exists():
        result = subprocess.run(
            [sys.executable, str(gen_script), "--board-root", str(board_root), "--mode", "apply"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"✅ {result.stdout.strip()}")
        else:
            print(f"⚠️ 索引刷新失败: {result.stderr}", file=sys.stderr)

    return 1 if scan_errors or write_errors else 0


def main():
    parser = argparse.ArgumentParser(
        description="检测租约超时任务，自动重排队或进 DLQ"
    )
    parser.add_argument(
        "--board-root",
        default=str(Path(__file__).parent),
        help="board 根目录路径（默认: 脚本所在目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告不修改",
    )
    parser.add_argument(
        "--mode",
        choices=["readonly", "apply"],
        default="readonly",
        help="readonly 只报告；apply 才执行 requeue/DLQ",
    )
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    return check_leases(board_root, dry_run=args.dry_run or args.mode != "apply")


if __name__ == "__main__":
    sys.exit(main())
