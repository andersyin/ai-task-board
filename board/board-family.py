#!/usr/bin/env python3
"""board-family — 任务家族管理工具（v1.3 新增）

用法:
  python3 board-family.py --family G-workbench-upgrade           # 输出家族树
  python3 board-family.py --family G-workbench-upgrade --health  # 健康检查
  python3 board-family.py --list                                  # 列出所有家族
  python3 board-family.py --family G-workbench-upgrade --freeze --reason "方向错误"  # 冻结家族
  python3 board-family.py --family G-workbench-upgrade --unfreeze  # 解冻家族
  python3 board-family.py --family G-workbench-upgrade --refresh   # 重建 family-status.json

退出码: 0=成功; 1=错误
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))

BOARD_ROOT = Path(__file__).parent.resolve()
KB_ROOT = BOARD_ROOT.parent.parent  # $KB_ROOT


def now_iso():
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def read_card_field(card_text: str, field: str) -> str | None:
    """从 card.md frontmatter 按行解析字段值。"""
    in_frontmatter = False
    for raw in card_text.splitlines():
        line = raw.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if not in_frontmatter:
            continue
        if line.startswith(f"{field}:"):
            val = line.split(":", 1)[1].strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    return None


def collect_family_tasks(family_id: str, board_root: Path) -> list[dict]:
    """收集指定家族的所有任务。"""
    tasks = []
    tasks_dir = board_root / "tasks"
    if not tasks_dir.exists():
        return tasks
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        card_path = task_dir / "card.md"
        status_path = task_dir / "status.json"
        if not card_path.exists() or not status_path.exists():
            continue
        try:
            card_text = card_path.read_text(encoding="utf-8")
            family = read_card_field(card_text, "family")
            if family != family_id:
                continue
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["_card_text"] = card_text
            status["_goal_alignment"] = read_card_field(card_text, "goal_alignment")
            status["_negative_constraints"] = read_card_field(card_text, "negative_constraints")
            # 提取标题
            for line in card_text.splitlines():
                if line.startswith("# "):
                    status["_title"] = line[2:].strip()
                    break
            tasks.append(status)
        except (OSError, json.JSONDecodeError):
            continue
    return tasks


def collect_all_families(board_root: Path) -> dict[str, list[dict]]:
    """收集所有家族及其任务。"""
    families = {}
    tasks_dir = board_root / "tasks"
    if not tasks_dir.exists():
        return families
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue
        card_path = task_dir / "card.md"
        status_path = task_dir / "status.json"
        if not card_path.exists() or not status_path.exists():
            continue
        try:
            card_text = card_path.read_text(encoding="utf-8")
            family = read_card_field(card_text, "family")
            if not family:
                continue
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["_goal_alignment"] = read_card_field(card_text, "goal_alignment")
            for line in card_text.splitlines():
                if line.startswith("# "):
                    status["_title"] = line[2:].strip()
                    break
            families.setdefault(family, []).append(status)
        except (OSError, json.JSONDecodeError):
            continue
    return families


def extract_directions(tasks: list[dict]) -> list[str]:
    """从任务的 goal_alignment 提取方向关键词。"""
    DIRECTION_PREFIXES = ["服务于", "实现", "完成", "按", "根据", "基于"]
    directions = []
    for t in tasks:
        ga = t.get("_goal_alignment", "")
        if not ga:
            continue
        cleaned = ga
        for prefix in DIRECTION_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break
        cleaned = cleaned.strip("，。、,. ")
        directions.append(cleaned[:20])
    return directions


def read_family_status(family_id: str, board_root: Path) -> dict:
    """读取家族状态。无 status.json 返回默认 active 状态。"""
    status_path = board_root / "families" / family_id / "status.json"
    if not status_path.exists():
        return {
            "family_id": family_id,
            "status": "active",
            "task_count": 0,
            "max_depth": 0,
            "rejections": [],
            "directions_detected": [],
            "direction_split": False,
            "last_health_check": None,
        }
    return json.loads(status_path.read_text(encoding="utf-8"))


def write_family_status(family_id: str, board_root: Path, status: dict):
    """写入家族状态。"""
    families_dir = board_root / "families" / family_id
    families_dir.mkdir(parents=True, exist_ok=True)
    status_path = families_dir / "status.json"
    status_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def print_family_tree(family_id: str, board_root: Path):
    """输出家族树。"""
    tasks = collect_family_tasks(family_id, board_root)
    family_status = read_family_status(family_id, board_root)

    print(f"\n{'='*60}")
    print(f"家族: {family_id}")
    print(f"状态: {family_status.get('status', 'active')}")
    print(f"任务数: {len(tasks)}")
    print(f"{'='*60}")

    # 根目标信息
    root_goal_card = board_root / "tasks" / family_id / "card.md"
    if root_goal_card.exists():
        try:
            root_text = root_goal_card.read_text(encoding="utf-8")
            for line in root_text.splitlines():
                if line.startswith("# "):
                    print(f"根目标: {line[2:].strip()}")
                    break
            confirmed = read_card_field(root_text, "confirmed_by")
            if confirmed:
                print(f"确认人: {confirmed}")
        except OSError:
            pass

    # 被拒记录
    rejections = family_status.get("rejections", [])
    if rejections:
        print(f"\n被拒记录 ({len(rejections)} 次):")
        for r in rejections:
            print(f"  - {r.get('task_id', '?')}: {r.get('reason', '?')} ({r.get('rejected_at', '?')})")

    # 任务列表
    print(f"\n任务列表 ({len(tasks)} 个):")
    for t in tasks:
        title = t.get("_title", t.get("id", "?"))
        status = t.get("status", "?")
        ga = t.get("_goal_alignment", "")
        complexity = t.get("complexity", "?")
        print(f"  [{status:>8}] {t['id']} ({complexity}) — {title}")
        if ga:
            print(f"            对齐: {ga}")

    # 方向分裂检测
    directions = extract_directions(tasks)
    if directions:
        unique = list(set(directions))
        if len(unique) > 1:
            print(f"\n🔴 方向分裂检测: {unique}")
        else:
            print(f"\n✅ 方向一致: {unique[0]}")

    print()


def print_health_report(family_id: str, board_root: Path):
    """生成并输出健康报告。"""
    tasks = collect_family_tasks(family_id, board_root)
    family_status = read_family_status(family_id, board_root)

    print(f"\n{'='*60}")
    print(f"家族健康报告: {family_id}")
    print(f"{'='*60}")

    # 基础指标
    status_counts = {}
    for t in tasks:
        s = t.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    print(f"\n任务总数: {len(tasks)}")
    print(f"状态分布: {status_counts}")
    print(f"家族状态: {family_status.get('status', 'active')}")

    # 检查点7：家族规模阈值
    if len(tasks) > 5:
        print(f"📋 警告: 任务数 {len(tasks)} > 5，建议检查是否任务拆分过细")

    # 检查 goal_alignment 完整性
    missing_ga = [t for t in tasks if not t.get("_goal_alignment")]
    if missing_ga:
        print(f"⚠️ 缺少 goal_alignment 的任务: {[t['id'] for t in missing_ga]}")

    # 方向一致性
    directions = extract_directions(tasks)
    if directions:
        unique = list(set(directions))
        if len(unique) > 1:
            print(f"🔴 方向分裂: {unique}")
        else:
            print(f"✅ 方向一致: {unique[0]}")

    # 被拒记录
    rejections = family_status.get("rejections", [])
    if rejections:
        print(f"\n被拒记录: {len(rejections)} 次")
        for r in rejections:
            print(f"  - {r.get('task_id', '?')}: {r.get('reason', '?')}")

    # 冻结家族下有非 decision 任务仍 open/claimed
    if family_status.get("status") == "frozen":
        active_tasks = [t for t in tasks if t.get("status") in ("open", "claimed") and t.get("task_type") != "decision"]
        if active_tasks:
            print(f"🔴 家族已冻结但以下任务仍 open/claimed: {[t['id'] for t in active_tasks]}")

    # 更新 family-status.json
    family_status["task_count"] = len(tasks)
    family_status["directions_detected"] = list(set(extract_directions(tasks)))
    family_status["direction_split"] = len(set(extract_directions(tasks))) > 1
    family_status["last_health_check"] = now_iso()
    write_family_status(family_id, board_root, family_status)
    print(f"\nfamily-status.json 已更新")
    print()


def freeze_family(family_id: str, reason: str, board_root: Path):
    """冻结家族 + 创建目标重新对齐 decision 任务。"""
    family_status = read_family_status(family_id, board_root)
    family_status["status"] = "frozen"
    family_status.setdefault("rejections", []).append({
        "task_id": None,
        "rejected_at": now_iso(),
        "reason": reason,
        "rejected_by": "user",
    })
    write_family_status(family_id, board_root, family_status)
    print(f"✅ 家族 {family_id} 已冻结")
    print(f"   原因: {reason}")
    print(f"   该家族下所有 open 任务不可认领，claimed 任务 lease 到期后不可重新认领")

    # 自动创建"目标重新对齐" decision 任务
    decision_task_id = f"{family_id}-realignment"
    decision_task_dir = board_root / "tasks" / decision_task_id
    if not decision_task_dir.exists():
        create_script = board_root / "board-task-create.py"
        if create_script.exists():
            result = subprocess.run(
                [sys.executable, str(create_script),
                 "--id", decision_task_id,
                 "--title", f"目标重新对齐: {family_id}",
                 "--complexity", "L1",
                 "--required-caps", "trae,workbuddy,codex,antigravity,qwenwork",
                 "--created-by", "trae",
                 "--body", f"家族 {family_id} 已冻结（原因: {reason}）。用户需确认新方向后才可解锁。\n\n请用户确认：\n1. 新方向是什么\n2. 哪些已有任务保留\n3. 哪些已有任务废弃",
                 "--work-key", f"family.{family_id}.realignment",
                 "--acceptance", "用户明确确认新的家族方向与保留/废弃范围",
                 "--output", "家族重新对齐决策",
                 "--evidence", "用户审批证据与 activate 事件",
                 "--type", "decision",
                 "--priority", "urgent",
                 "--board-root", str(board_root)],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"✅ 已创建 decision 任务: {decision_task_id}")
            else:
                print(f"⚠️ decision 任务创建失败: {result.stderr}")
    else:
        print(f"ℹ️ decision 任务已存在: {decision_task_id}")


def unfreeze_family(family_id: str, board_root: Path):
    """解冻家族。"""
    family_status = read_family_status(family_id, board_root)
    family_status["status"] = "active"
    write_family_status(family_id, board_root, family_status)
    print(f"✅ 家族 {family_id} 已解冻")
    print(f"   被拒记录保留: {len(family_status.get('rejections', []))} 次")


def refresh_family(family_id: str, board_root: Path):
    """重建 family-status.json。"""
    tasks = collect_family_tasks(family_id, board_root)
    existing = read_family_status(family_id, board_root)

    family_status = {
        "family_id": family_id,
        "status": existing.get("status", "active"),
        "task_count": len(tasks),
        "max_depth": 1,  # 简化：目前只有根目标→执行任务一层
        "rejections": existing.get("rejections", []),
        "directions_detected": list(set(extract_directions(tasks))),
        "direction_split": len(set(extract_directions(tasks))) > 1,
        "last_health_check": now_iso(),
    }
    write_family_status(family_id, board_root, family_status)
    print(f"✅ family-status.json 已重建: {family_id}")
    print(f"   任务数: {len(tasks)}")
    print(f"   方向: {family_status['directions_detected']}")
    if family_status["direction_split"]:
        print(f"   🔴 方向分裂!")


def list_families(board_root: Path):
    """列出所有家族。"""
    families = collect_all_families(board_root)
    if not families:
        print("暂无家族（所有任务均无 family 字段）")
        return

    print(f"\n{'='*60}")
    print(f"家族列表 ({len(families)} 个)")
    print(f"{'='*60}\n")
    for family_id, tasks in families.items():
        family_status = read_family_status(family_id, board_root)
        fs = family_status.get("status", "active")
        task_count = len(tasks)
        directions = extract_directions(tasks)
        unique_dirs = list(set(directions))
        split = "🔴 分裂" if len(unique_dirs) > 1 else "✅ 一致"
        print(f"  {family_id}")
        print(f"    状态: {fs} | 任务数: {task_count} | 方向: {split}")
        if unique_dirs:
            print(f"    方向: {unique_dirs}")
        print()


def main():
    parser = argparse.ArgumentParser(description="任务家族管理工具")
    parser.add_argument("--family", default="", help="家族 ID")
    parser.add_argument("--health", action="store_true", help="生成健康检查报告")
    parser.add_argument("--list", action="store_true", help="列出所有家族")
    parser.add_argument("--freeze", action="store_true", help="冻结家族")
    parser.add_argument("--unfreeze", action="store_true", help="解冻家族")
    parser.add_argument("--refresh", action="store_true", help="重建 family-status.json")
    parser.add_argument("--reason", default="", help="冻结原因")
    parser.add_argument("--board-root", default=str(BOARD_ROOT), help="board 根目录")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()

    if args.list:
        list_families(board_root)
        return 0

    if not args.family:
        print("❌ 请指定 --family <ID> 或 --list", file=sys.stderr)
        return 1

    if args.freeze:
        if not args.reason:
            print("❌ --freeze 需要 --reason 参数", file=sys.stderr)
            return 1
        freeze_family(args.family, args.reason, board_root)
    elif args.unfreeze:
        unfreeze_family(args.family, board_root)
    elif args.refresh:
        refresh_family(args.family, board_root)
    elif args.health:
        print_health_report(args.family, board_root)
    else:
        print_family_tree(args.family, board_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
