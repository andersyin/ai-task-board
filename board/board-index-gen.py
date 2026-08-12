#!/usr/bin/env python3
"""board-index-gen — 确定性扫描 tasks/ 目录，生成 board-index.json（v1.1：支持项目/依赖/eligible）。

用法: python3 board-index-gen.py [--board-root <path>]
退出码: 0=成功; 1=错误
只读: 只读 tasks/*/status.json 和 card.md，生成 board-index.json。

v1.1 新增:
  - project, iteration, priority, depends_on 字段
  - eligible 字段（依赖未完成的任务标记为 false）
  - review_result, review_round, review_issues_count 字段
  - requires_governance_approval 字段

v1.2 新增:
  - open 任务按 priority 排序（urgent > high > normal > low）
  - agent_confidence 段（基于 REVIEW.md 统计各端 PASS/FAIL 置信度）
  - card_hash 字段
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from board_integrity import get_task_integrity
from board_contract import atomic_write_json, board_lock, canonical_agent, is_physical_task_dir
from board_verdicts import parse_legacy_verdict

try:
    import yaml
except ImportError:
    yaml = None


def _fallback_frontmatter(raw):
    """无 PyYAML 时解析索引实际使用的两个简单字段。"""
    meta = {}
    for source_line in raw.splitlines():
        line = source_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in {"required_caps", "task_type"}:
            continue
        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].strip()
            meta[key] = [
                item.strip().strip("'\"")
                for item in items.split(",")
                if item.strip()
            ]
        else:
            meta[key] = value.strip("'\"")
    return meta


def parse_frontmatter(text):
    """从 markdown 文本解析 YAML frontmatter。"""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    if yaml is None:
        return _fallback_frontmatter(parts[1])
    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}


def scan_task(task_dir, board_root, all_task_statuses):
    """扫描单个任务目录，返回任务元数据字典。"""
    status_path = task_dir / "status.json"
    card_path = task_dir / "card.md"

    if not status_path.exists():
        return None

    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ 跳过 {task_dir.name}: status.json 解析失败: {e}", file=sys.stderr)
        return None

    # 从 card.md 补充 required_caps
    meta = {}
    if card_path.exists():
        meta = parse_frontmatter(card_path.read_text(encoding="utf-8"))

    task_id = task_dir.name
    required_caps = status.get("required_caps") or meta.get("required_caps", [])

    # v1.1: eligible 检查（依赖是否全部完成）
    depends_on = status.get("depends_on", [])
    eligible = True
    if depends_on:
        for dep_id in depends_on:
            dep_status = all_task_statuses.get(dep_id, {})
            if dep_status.get("status") != "done":
                eligible = False
                break

    result = {
        "id": task_id,
        "status": status.get("status", "open"),
        "required_caps": required_caps,
        "card": f"tasks/{task_dir.name}/card.md",
        "executor": status.get("executor"),
        "executor_model": status.get("executor_model"),
        "reviewer": status.get("reviewer"),
        "reviewer_model": status.get("reviewer_model"),
        "complexity": status.get("complexity", "L1"),
        "requeue_count": status.get("requeue_count", 0),
        "created_by": status.get("created_by"),
        "created": status.get("created"),
        "created_at": status.get("created_at"),
        "claimed_at": status.get("claimed_at"),
        "lease_expires_at": status.get("lease_expires_at"),
        "completed_at": status.get("completed_at"),
        "contract_version": status.get("contract_version"),
    }

    integrity = get_task_integrity(task_dir, status)
    result["integrity_status"] = integrity.status
    if integrity.quarantined:
        result["quarantined"] = True
        result["integrity_error"] = integrity.detail
        result["eligible"] = False

    # v1.1 新增字段（向后兼容：只在 status.json 有值时写入）
    if status.get("project"):
        result["project"] = status["project"]
    if status.get("iteration"):
        result["iteration"] = status["iteration"]
    if status.get("priority"):
        result["priority"] = status["priority"]
    if depends_on:
        result["depends_on"] = depends_on
        result["eligible"] = eligible
    if status.get("requires_governance_approval"):
        result["requires_governance_approval"] = True
    if status.get("task_type"):
        result["task_type"] = status["task_type"]
    elif meta.get("task_type"):
        result["task_type"] = meta["task_type"]
    if status.get("review_result"):
        result["review_result"] = status["review_result"]
    if status.get("review_round", 0) > 0:
        result["review_round"] = status["review_round"]
    if status.get("review_issues_count") is not None:
        result["review_issues_count"] = status["review_issues_count"]

    # v1.2: card_hash（完整性校验用）
    if status.get("card_hash"):
        result["card_hash"] = status["card_hash"]

    for identity_field in (
        "work_key",
        "root_work_id",
        "continuation_of",
        "follow_up_of",
        "supersedes",
        "superseded_by",
        "contract_fingerprint",
        "submission_contract_fingerprint",
        "decision_activated_at",
        "disposition",
        "cancelled_at",
        "superseded_at",
        "archived_at",
        "archived_from",
    ):
        if status.get(identity_field):
            result[identity_field] = status[identity_field]

    return result


# v1.2: 优先级排序映射
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def compute_agent_confidence(tasks_root):
    """扫描所有 REVIEW.md，统计各端 PASS/FAIL 次数，计算置信度。"""
    stats = {}  # {agent_name: {"pass": N, "fail": N, "total": N}}

    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")) or not is_physical_task_dir(task_dir, tasks_root):
            continue
        review_path = task_dir / "REVIEW.md"
        if not review_path.exists():
            continue

        status_path = task_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        executor = canonical_agent(str(status.get("executor") or "").split("/", 1)[0])
        reviewer = status.get("reviewer")
        if not executor or not reviewer:
            continue

        review_text = review_path.read_text(encoding="utf-8")
        # 新契约只有 pass/fail；旧结果只用于历史统计兼容。
        verdict = parse_legacy_verdict(review_text)
        if not verdict:
            continue

        result = "fail" if verdict == "fail" else "pass"

        if executor not in stats:
            stats[executor] = {"pass": 0, "fail": 0, "total": 0}
        stats[executor][result] += 1
        stats[executor]["total"] += 1

        # REVIEW 结果评价执行产物，不评价 reviewer；否则 reviewer 抓出 FAIL
        # 反而会被降置信度，造成指标方向颠倒。

    confidence = {}
    for agent, s in stats.items():
        if s["total"] > 0:
            rate = round(s["pass"] / s["total"] * 100, 1)
        else:
            rate = 0
        confidence[agent] = {
            "pass": s["pass"],
            "fail": s["fail"],
            "total": s["total"],
            "pass_rate": rate,
            "level": "high" if rate >= 80 else ("medium" if rate >= 50 else "low"),
        }

    return confidence


def sort_tasks_by_priority(tasks):
    """v1.2: open 任务按 priority 排序，其他状态保持原序。"""
    open_tasks = [t for t in tasks if t["status"] == "open"]
    other_tasks = [t for t in tasks if t["status"] != "open"]

    open_tasks.sort(
        key=lambda t: (
            PRIORITY_RANK.get(t.get("priority", "normal"), 2),
            t.get("created", ""),
        )
    )

    return open_tasks + other_tasks


def main():
    parser = argparse.ArgumentParser(
        description="确定性扫描 tasks/ 目录，生成 board-index.json（v1.1）"
    )
    parser.add_argument(
        "--board-root",
        default=str(Path(__file__).parent),
        help="board 根目录路径（默认: 脚本所在目录）",
    )
    parser.add_argument(
        "--mode",
        choices=["readonly", "apply"],
        default="readonly",
        help="readonly 只计算；apply 才写 board-index.json",
    )
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"

    if not tasks_root.exists():
        print(f"❌ tasks 目录不存在: {tasks_root}", file=sys.stderr)
        return 1

    # 序列化整轮扫描；配合原子 replace，防止并发生成写坏或旧快照后写覆盖。
    index_lock = None
    if args.mode == "apply":
        index_lock = board_lock(board_root, "index")
        index_lock.__enter__()

    # 第一遍：收集所有 status.json 用于 eligible 检查
    all_task_statuses = {}
    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")) or not is_physical_task_dir(task_dir, tasks_root):
            continue
        status_path = task_dir / "status.json"
        if status_path.exists():
            try:
                st = json.loads(status_path.read_text(encoding="utf-8"))
                all_task_statuses[task_dir.name] = st
            except (json.JSONDecodeError, OSError):
                pass

    # 第二遍：扫描并生成索引
    tasks = []
    for task_dir in sorted(tasks_root.iterdir()):
        if task_dir.name.startswith((".", "_")) or not is_physical_task_dir(task_dir, tasks_root):
            continue
        task = scan_task(task_dir, board_root, all_task_statuses)
        if task:
            tasks.append(task)

    # v1.2: 按 priority 排序 open 任务
    tasks = sort_tasks_by_priority(tasks)

    # v1.2: 计算端置信度
    agent_confidence = compute_agent_confidence(tasks_root)

    index = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": "board-index-gen.py v1.3 (锁定扫描 + 原子写，非 AI 写入)",
        "board_root": str(board_root),
        "task_count": len(tasks),
        "tasks": tasks,
    }
    if agent_confidence:
        index["agent_confidence"] = agent_confidence

    index_path = board_root / "board-index.json"
    if args.mode == "apply":
        atomic_write_json(index_path, index)

    # 按状态统计
    by_status = {}
    for t in tasks:
        s = t["status"]
        by_status[s] = by_status.get(s, 0) + 1

    # 按项目统计
    by_project = {}
    for t in tasks:
        p = t.get("project", "(无项目)")
        by_project[p] = by_project.get(p, 0) + 1

    if args.mode == "apply":
        print(f"✅ board-index.json 已生成: {index_path}")
    else:
        print("✅ readonly projection computed（未写 board-index.json）")
    print(f"   任务总数: {len(tasks)}")
    for s, c in sorted(by_status.items()):
        print(f"   {s}: {c}")
    if len(by_project) > 1 or "(无项目)" not in by_project:
        print(f"   项目分布:")
        for p, c in sorted(by_project.items()):
            if p != "(无项目)":
                print(f"     {p}: {c}")

    if index_lock is not None:
        index_lock.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
