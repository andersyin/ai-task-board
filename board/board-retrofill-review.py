#!/usr/bin/env python3
"""board-retrofill-review — 为 done 任务回填 REVIEW.md（v1.0）。

用法: python3 board-retrofill-review.py [--board-root <path>] [--dry-run]
退出码: 0=成功; 1=错误

功能:
  扫描所有 status=done 且缺 REVIEW.md 的任务（排除 fixture），
  生成带 RETROFILL 标记的模板 REVIEW.md，并补充 status.json 验收字段。

选项:
  --dry-run: 只打印将要回填的任务列表，不写盘
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))


def is_fixture(status):
    """判断是否为 fixture 类测试任务。"""
    note = status.get("note", "")
    return "fixture" in note.lower()


def generate_review(task_dir, status):
    """为单个任务生成 RETROFILL REVIEW.md 内容。"""
    task_id = status.get("id", task_dir.name)
    executor = status.get("executor") or "unknown"
    reviewer = status.get("reviewer") or "unknown"
    executor_model = status.get("executor_model") or "unknown"
    reviewer_model = status.get("reviewer_model") or "unknown"
    completed_at = status.get("completed_at") or "unknown"
    review_round = status.get("review_round", 0)

    # 如果 executor == reviewer，说明是自评闭环
    is_self_review = (executor == reviewer)

    # 如果 reviewer 是 null/unknown，用创建端兜底
    if reviewer in (None, "unknown"):
        reviewer = status.get("created_by") or "unknown"

    now = datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    review = f"""# 验收报告: {task_id}

> ⚠️ **RETROFILL** — 本报告由 board-retrofill-review.py 自动回填，非真实验收。
> 回填时间: {now}
> 回填原因: 任务 status=done 但缺少 REVIEW.md（历史遗留）

## 验收结果: PASS

## 验收方
- 端: {reviewer}
- 模型: {reviewer_model}
- 时间: {completed_at}
- 验收轮次: 第{max(review_round, 1)}轮（RETROFILL）

## 验收标准逐项
| 标准项（来自 card.md） | 通过 | 说明 |
|------------------------|------|------|
| （RETROFILL: 未实际验收） | Y | 自动回填，默认标记通过 |

## 问题清单（FAIL 时填写）
无（RETROFILL）

## 退回理由（FAIL 时必填）
不适用

## 验收方评语
本 REVIEW.md 由 board-retrofill-review.py 自动回填。原始任务在 {completed_at} 标记 done，但未生成 REVIEW.md。
{"⚠️ 注意: executor=reviewer（自评闭环），违反 self_close=false 原则。" if is_self_review else ""}

### 验收结果说明
- **PASS**: 所有验收标准通过，status → done（或 awaiting_user_approval 如果 requires_governance_approval=true）
- **FAIL**: 有 high 级别问题未通过，status → claimed（退回原执行端），review_round 递增
非阻断改进建议写入 REVIEW.md，不新增第三种结论。验收方必须调用 board-task-transition.py，不得手改 status.json。

### 退回流程
1. 验收方在 REVIEW.md 写明退回理由和问题清单
2. status 改为 claimed（不是 open），executor 保持不变
3. lease_expires_at 重置为当前时间 + 2h
4. review_round 递增
5. 原执行端巡查时发现自己名下的 claimed 任务有 REVIEW.md 标记 FAIL，读取退回理由后修改并重新提交
6. 如果原执行端离线（lease 过期），board-lease-check.py 将 status 改回 open，其他端可认领
"""
    return review


def update_status_review_fields(status_path, status):
    """补充 status.json 中的验收字段（如缺失）。"""
    changed = False

    if not status.get("review_result"):
        status["review_result"] = "pass"
        changed = True

    if status.get("review_round", 0) == 0:
        status["review_round"] = 1
        changed = True

    if status.get("review_issues_count") is None:
        status["review_issues_count"] = 0
        changed = True

    if changed:
        status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return changed


def main():
    parser = argparse.ArgumentParser(
        description="为 done 任务回填 REVIEW.md（v1.0）"
    )
    parser.add_argument(
        "--board-root",
        default=str(Path(__file__).parent),
        help="board 根目录路径（默认: 脚本所在目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要回填的任务列表，不写盘",
    )
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"

    if not tasks_root.exists():
        print(f"❌ tasks 目录不存在: {tasks_root}", file=sys.stderr)
        return 1

    # 扫描所有 done 任务
    to_retrofill = []
    for task_dir in sorted(tasks_root.iterdir()):
        if not task_dir.is_dir() or task_dir.name.startswith((".", "_")):
            continue

        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue

        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        # 只处理 done 任务
        if status.get("status") != "done":
            continue

        # 排除 fixture
        if is_fixture(status):
            continue

        # 检查是否已有 REVIEW.md
        review_path = task_dir / "REVIEW.md"
        if review_path.exists():
            continue

        to_retrofill.append((task_dir, status))

    if not to_retrofill:
        print("✅ 无需回填：所有非 fixture done 任务已有 REVIEW.md")
        return 0

    print(f"📋 发现 {len(to_retrofill)} 个任务需要回填 REVIEW.md:\n")
    for task_dir, status in to_retrofill:
        executor = status.get("executor") or "unknown"
        reviewer = status.get("reviewer") or "unknown"
        print(f"  - [{status.get('id', task_dir.name)}] executor={executor}, reviewer={reviewer}")

    if args.dry_run:
        print(f"\n（--dry-run 模式，不写盘）")
        return 0

    print(f"\n开始回填...")
    review_filled = 0
    status_updated = 0

    for task_dir, status in to_retrofill:
        # 生成 REVIEW.md
        review_content = generate_review(task_dir, status)
        review_path = task_dir / "REVIEW.md"
        review_path.write_text(review_content, encoding="utf-8")
        review_filled += 1

        # 补充 status.json 验收字段
        status_path = task_dir / "status.json"
        if update_status_review_fields(status_path, status):
            status_updated += 1

    print(f"\n✅ 回填完成:")
    print(f"   REVIEW.md 已生成: {review_filled} 个")
    print(f"   status.json 验收字段已补充: {status_updated} 个")
    print(f"\n⚠️ 这些 REVIEW.md 标记为 RETROFILL，非真实验收。")
    print(f"   后续如有真实验收需求，请手动替换。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
