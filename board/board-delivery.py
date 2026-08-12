#!/usr/bin/env python3
"""board-delivery — 最小化项目交付追踪（收敛改造 v1.0）。

核心区分：task done ≠ project delivered。
任务 done 只表示"这个任务做完了"，项目 delivered 表示"真实验收标准达成"。

用法:
  python3 board-delivery.py init --project workbench-final --milestone "回归测试通过、无P0、核心交互可用"
  python3 board-delivery.py status --project workbench-final
  python3 board-delivery.py deliver --project workbench-final --evidence "DECISIONS 2026-08-10"
  python3 board-delivery.py abandon --project workbench-final --reason "放弃，原因..."
  python3 board-delivery.py list
  python3 board-delivery.py check --project workbench-final   # 检查是否所有任务 done

设计原则：
- 不新增状态机状态，复用 task 的 done/abandoned 和 project 的 in_progress/delivered/abandoned
- 不新增 Agent、Validator、Wake 机制
- 替代 board-family.py 的家族/注意力机制（复杂度做减法）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
PROJECTS_FILE = "projects.json"


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def load_projects(board_root: Path) -> dict:
    path = board_root / PROJECTS_FILE
    if not path.exists():
        return {"projects": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"projects": {}}


def save_projects(board_root: Path, data: dict) -> None:
    path = board_root / PROJECTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_task_statuses(board_root: Path) -> list[dict]:
    """加载所有任务的 status.json。"""
    tasks_root = board_root / "tasks"
    result = []
    if not tasks_root.is_dir():
        return result
    for task_dir in sorted(tasks_root.iterdir()):
        status_path = task_dir / "status.json"
        if not status_path.is_file():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            result.append(status)
        except (OSError, json.JSONDecodeError):
            continue
    return result


def project_task_stats(board_root: Path, project: str) -> dict:
    """统计项目下任务的状态分布。"""
    statuses = load_task_statuses(board_root)
    project_tasks = [s for s in statuses if s.get("project") == project]
    return {
        "total": len(project_tasks),
        "done": sum(1 for s in project_tasks if s.get("status") == "done"),
        "active": sum(1 for s in project_tasks if s.get("status") in ("open", "claimed", "awaiting_review", "awaiting_user_approval", "blocked")),
        "dlq": sum(1 for s in project_tasks if s.get("status") == "dlq"),
        "tasks": [{"id": s.get("id"), "status": s.get("status")} for s in project_tasks],
    }


def cmd_init(args, board_root: Path) -> int:
    data = load_projects(board_root)
    project = args.project
    if not project or not project.replace("-", "").replace("_", "").replace(".", "").isalnum():
        print("❌ project 名称只允许字母数字/._-", file=sys.stderr)
        return 1
    data["projects"][project] = {
        "delivery_milestone": args.milestone,
        "delivery_status": "in_progress",
        "created_at": now_iso(),
        "delivered_at": None,
        "abandoned_at": None,
        "abandon_reason": None,
    }
    save_projects(board_root, data)
    print(f"✅ 项目已初始化: {project}")
    print(f"   交付标准: {args.milestone}")
    print(f"   交付状态: in_progress")
    return 0


def cmd_status(args, board_root: Path) -> int:
    data = load_projects(board_root)
    project = args.project
    info = data.get("projects", {}).get(project)
    if not info:
        print(f"❌ 项目不存在: {project}", file=sys.stderr)
        return 1
    stats = project_task_stats(board_root, project)
    print(f"项目: {project}")
    print(f"  交付标准: {info.get('delivery_milestone')}")
    print(f"  交付状态: {info.get('delivery_status')}")
    print(f"  任务统计: total={stats['total']} done={stats['done']} active={stats['active']} dlq={stats['dlq']}")
    if stats["tasks"]:
        print(f"  任务列表:")
        for t in stats["tasks"]:
            print(f"    {t['id']}: {t['status']}")
    all_done = stats["active"] == 0 and stats["dlq"] == 0 and stats["total"] > 0
    print(f"  所有任务已结束: {'是' if all_done else '否'}")
    return 0


def cmd_list(args, board_root: Path) -> int:
    data = load_projects(board_root)
    projects = data.get("projects", {})
    if not projects:
        print("（无已注册项目）")
        return 0
    for name, info in sorted(projects.items()):
        stats = project_task_stats(board_root, name)
        print(f"{name}: {info.get('delivery_status')} (done={stats['done']}/{stats['total']}) — {info.get('delivery_milestone', '')}")
    return 0


def cmd_deliver(args, board_root: Path) -> int:
    data = load_projects(board_root)
    project = args.project
    info = data.get("projects", {}).get(project)
    if not info:
        print(f"❌ 项目不存在: {project}", file=sys.stderr)
        return 1
    if info.get("delivery_status") != "in_progress":
        print(f"❌ 项目当前状态为 {info.get('delivery_status')}，不可标记 delivered", file=sys.stderr)
        return 1
    stats = project_task_stats(board_root, project)
    all_done = stats["active"] == 0 and stats["dlq"] == 0 and stats["total"] > 0
    if not all_done and not args.force:
        print(f"❌ 项目尚有 {stats['active']} 个活跃任务和 {stats['dlq']} 个死信任务，不可标记 delivered", file=sys.stderr)
        print(f"   使用 --force 强制标记（需自行确认验收标准已达成）", file=sys.stderr)
        return 1
    info["delivery_status"] = "delivered"
    info["delivered_at"] = now_iso()
    info["delivery_evidence"] = args.evidence
    save_projects(board_root, data)
    print(f"✅ 项目已标记 delivered: {project}")
    print(f"   交付标准: {info.get('delivery_milestone')}")
    print(f"   证据: {args.evidence}")
    return 0


def cmd_abandon(args, board_root: Path) -> int:
    data = load_projects(board_root)
    project = args.project
    info = data.get("projects", {}).get(project)
    if not info:
        print(f"❌ 项目不存在: {project}", file=sys.stderr)
        return 1
    info["delivery_status"] = "abandoned"
    info["abandoned_at"] = now_iso()
    info["abandon_reason"] = args.reason
    save_projects(board_root, data)
    print(f"✅ 项目已标记 abandoned: {project}")
    print(f"   原因: {args.reason}")
    return 0


def cmd_check(args, board_root: Path) -> int:
    """检查项目是否所有任务已结束（done/dlq）。"""
    stats = project_task_stats(board_root, args.project)
    all_done = stats["active"] == 0 and stats["dlq"] == 0 and stats["total"] > 0
    result = {
        "project": args.project,
        "all_tasks_done": all_done,
        "total": stats["total"],
        "done": stats["done"],
        "active": stats["active"],
        "dlq": stats["dlq"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_done else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="项目交付追踪（收敛改造）")
    sub = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--board-root", default=str(Path(__file__).parent))

    p_init = sub.add_parser("init", help="初始化项目交付标准")
    p_init.add_argument("--project", required=True)
    p_init.add_argument("--milestone", required=True, help="交付标准描述")

    p_status = sub.add_parser("status", help="查看项目交付状态")
    p_status.add_argument("--project", required=True)

    p_list = sub.add_parser("list", help="列出所有项目")

    p_deliver = sub.add_parser("deliver", help="标记项目已交付")
    p_deliver.add_argument("--project", required=True)
    p_deliver.add_argument("--evidence", required=True, help="交付证据（如 DECISIONS 日期）")
    p_deliver.add_argument("--force", action="store_true", help="强制标记（跳过活跃任务检查）")

    p_abandon = sub.add_parser("abandon", help="标记项目放弃")
    p_abandon.add_argument("--project", required=True)
    p_abandon.add_argument("--reason", required=True)

    p_check = sub.add_parser("check", help="检查项目所有任务是否已结束")
    p_check.add_argument("--project", required=True)

    args = parser.parse_args()
    board_root = Path(args.board_root).resolve()

    if args.command == "init":
        return cmd_init(args, board_root)
    elif args.command == "status":
        return cmd_status(args, board_root)
    elif args.command == "list":
        return cmd_list(args, board_root)
    elif args.command == "deliver":
        return cmd_deliver(args, board_root)
    elif args.command == "abandon":
        return cmd_abandon(args, board_root)
    elif args.command == "check":
        return cmd_check(args, board_root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
