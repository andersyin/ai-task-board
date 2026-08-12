#!/usr/bin/env python3
"""项目交付状态追踪（v1.0：最小化收敛版）。

新增功能：
- 项目级 delivery_milestone（验收标准）和 delivery_status（交付状态）
- 项目任务统计与收敛判定
- 强制停止条件检测（review fail ≥2 / 同项目 8 任务未交付 / 同问题返工 ≥3）
- 元任务预算（active meta tasks ≤ 20% active tasks）

不做的事：
- 不新增 Agent、Validator、状态、Wake 机制
- 不新增治理检查点
- 不优化架构完整性
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

CN_TZ = timezone(timedelta(hours=8))

# 元任务关键词（卡标题或 project 包含这些词即视为元任务）
META_KEYWORDS = [
    "board", "validator", "orchestration", "wake", "governance",
    "review-framework", "system", "optimization", "audit",
    "重构", "优化", "系统", "架构", "机制",
]

# 强制停止条件阈值
FAIL_THRESHOLD = 2  # 同任务 review fail ≥ 2 次
UNDELIVERED_TASK_THRESHOLD = 8  # 同项目约 8 个任务未交付
REWORK_THRESHOLD = 3  # 同一问题返工 ≥ 3 次


def load_tasks(board_root: Path) -> List[Dict]:
    """加载所有任务状态。"""
    tasks_root = board_root / "tasks"
    tasks = []
    for task_dir in tasks_root.iterdir():
        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            tasks.append(status)
        except (json.JSONDecodeError, OSError):
            continue
    return tasks


def is_meta_task(task: Dict) -> bool:
    """判断是否为元任务。"""
    title = task.get("id", "")
    project = task.get("project", "")
    title_lower = title.lower()
    project_lower = project.lower()
    return any(kw in title_lower or kw in project_lower for kw in META_KEYWORDS)


def compute_project_stats(tasks: List[Dict]) -> Dict[str, Dict]:
    """计算项目级统计。"""
    stats = {}
    for task in tasks:
        project = task.get("project") or "(无项目)"
        if project not in stats:
            stats[project] = {
                "total": 0,
                "open": 0,
                "claimed": 0,
                "awaiting_review": 0,
                "done": 0,
                "undelivered": 0,
                "meta_count": 0,
                "non_meta_count": 0,
                "review_fail_counts": {},  # task_id -> fail count
                "rework_tracking": {},  # issue_key -> fail count
            }
        s = stats[project]
        s["total"] += 1
        status = task.get("status")
        if status == "open":
            s["open"] += 1
            s["undelivered"] += 1
        elif status == "claimed":
            s["claimed"] += 1
            s["undelivered"] += 1
        elif status == "awaiting_review":
            s["awaiting_review"] += 1
            s["undelivered"] += 1
        elif status == "done":
            s["done"] += 1

        # 计数 meta vs 非元任务（只计算活跃任务：open + claimed + awaiting_review）
        if status in ("open", "claimed", "awaiting_review"):
            if is_meta_task(task):
                s["meta_count"] += 1
            else:
                s["non_meta_count"] += 1

        # 追踪 review fail 次数
        task_id = task.get("id")
        review_result = task.get("review_result")
        if review_result == "fail":
            s["review_fail_counts"][task_id] = s["review_fail_counts"].get(task_id, 0) + 1

        # 返工追踪只统计活跃工作；历史 done 不能反复触发当前停止条件。
        if status in ("open", "claimed", "awaiting_review", "blocked"):
            for keyword in ["oscillation", "hover", "click", "layout", "regression"]:
                if keyword in task_id.lower():
                    issue_key = keyword
                    s["rework_tracking"][issue_key] = s["rework_tracking"].get(issue_key, 0) + 1

    return stats


def check_stop_conditions(project: str, stats: Dict, tasks: List[Dict]) -> List[str]:
    """检查强制停止条件。"""
    issues = []

    # 条件1：同一任务 review fail ≥ 2 次
    for task_id, fail_count in stats["review_fail_counts"].items():
        if fail_count >= FAIL_THRESHOLD:
            issues.append(f"🔴 同任务 {task_id} review fail 已达 {fail_count} 次，停止自动继续拆任务")

    # 条件2：同项目累计产生约 8 个任务仍未交付
    undelivered = stats["undelivered"]
    if undelivered >= UNDELIVERED_TASK_THRESHOLD:
        issues.append(f"🔴 项目 {project} 已有 {undelivered} 个任务未交付（阈值 {UNDELIVERED_TASK_THRESHOLD}），提交用户裁决")

    # 条件3：同一问题连续返工 ≥ 3 次
    for issue_key, count in stats["rework_tracking"].items():
        if count >= REWORK_THRESHOLD:
            issues.append(f"🔴 项目 {project} 问题 '{issue_key}' 返工已达 {count} 次（阈值 {REWORK_THRESHOLD}），提交用户裁决")

    return issues


def check_meta_budget(stats: Dict, project: str) -> List[str]:
    """检查元任务预算。"""
    issues = []
    total_active = stats["meta_count"] + stats["non_meta_count"]
    if total_active == 0:
        return issues

    meta_ratio = stats["meta_count"] / total_active
    if meta_ratio > 0.20:
        issues.append(
            f"🟡 项目 {project} 元任务占比 {meta_ratio*100:.1f}%（{stats['meta_count']}/{total_active}），"
            f"超过 20% 阈值。禁止创建新的 board/validator/orchestration/wake/governance/review-framework 任务"
        )
    return issues


def generate_report(board_root: Path) -> Dict:
    """生成项目交付状态报告。"""
    tasks = load_tasks(board_root)
    project_stats = compute_project_stats(tasks)

    report = {
        "generated": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "generator": "project-delivery-tracker.py v1.0 (收敛改造版)",
        "board_root": str(board_root),
        "total_tasks": len(tasks),
        "projects": {},
        "global_meta_budget": {
            "total_active": 0,
            "total_meta": 0,
            "meta_ratio": 0.0,
            "within_budget": True,
        },
    }

    all_active = 0
    all_meta = 0

    for project, stats in project_stats.items():
        stop_issues = check_stop_conditions(project, stats, tasks)
        meta_issues = check_meta_budget(stats, project)

        project_report = {
            "total_tasks": stats["total"],
            "open": stats["open"],
            "claimed": stats["claimed"],
            "awaiting_review": stats["awaiting_review"],
            "done": stats["done"],
            "undelivered": stats["undelivered"],
            "active_meta_tasks": stats["meta_count"],
            "active_non_meta_tasks": stats["non_meta_count"],
            "delivery_status": "delivered" if stats["undelivered"] == 0 and stats["done"] > 0 else "not_delivered",
            "stop_conditions_triggered": stop_issues,
            "meta_budget_issues": meta_issues,
        }

        report["projects"][project] = project_report

        all_active += stats["meta_count"] + stats["non_meta_count"]
        all_meta += stats["meta_count"]

    # 全局元任务预算检查
    if all_active > 0:
        report["global_meta_budget"]["total_active"] = all_active
        report["global_meta_budget"]["total_meta"] = all_meta
        report["global_meta_budget"]["meta_ratio"] = round(all_meta / all_active, 3)
        report["global_meta_budget"]["within_budget"] = (all_meta / all_active) <= 0.20

    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description="项目交付状态追踪（收敛改造版）")
    parser.add_argument("--board-root", default=str(Path(__file__).parent), help="board 根目录")
    parser.add_argument("--check-meta-create", help="检查是否允许创建元任务（传入任务名）")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    report = generate_report(board_root)

    if args.check_meta_create:
        # 检查是否允许创建新元任务
        task_name = args.check_meta_create.lower()
        is_meta = any(kw in task_name for kw in META_KEYWORDS)
        if not is_meta:
            print("✅ 允许创建：非元任务")
            return 0

        if report["global_meta_budget"]["within_budget"]:
            print(f"✅ 允许创建：元任务预算内（{report['global_meta_budget']['meta_ratio']*100:.1f}%）")
            return 0
        else:
            print(
                f"❌ 禁止创建：元任务超预算（{report['global_meta_budget']['meta_ratio']*100:.1f}%），"
                f"禁止创建新的 board/validator/orchestration/wake/governance/review-framework 任务"
            )
            return 1
    else:
        # 输出完整报告
        print(json.dumps(report, ensure_ascii=False, indent=2))

        # 检查是否有强制停止条件触发
        has_stop_issues = any(
            p["stop_conditions_triggered"]
            for p in report["projects"].values()
        )
        if has_stop_issues:
            return 1  # 非零退出码表示需要人工干预

        return 0


if __name__ == "__main__":
    sys.exit(main())
