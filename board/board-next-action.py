#!/usr/bin/env python3
"""为注册端选择一个确定性的公告牌下一动作（只读，不修改任务状态）。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_integrity import get_task_integrity
from board_contract import (
    AGENT_CONTRACTS,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    canonical_agent,
    eligible_reviewers,
    effective_execute_level,
    is_physical_task_dir,
    level_value,
    requires_independent_review,
    validate_model_label,
)
from board_transition import (
    TransitionError,
    review_authorization_is_event_backed,
    validate_reviewer,
)
from board_reconcile_contract import RECONCILE_PENDING_FILE


CN_TZ = timezone(timedelta(hours=8))
PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
WAKE_DISPATCHER = "qwenwork"
WAKE_TARGET_MODELS = {
    "workbuddy": "deepseek-v4-flash",
    "trae": "glm-5.2",
    "antigravity": "gemini-3.6-flash",
    "codex": "gpt-5.6-sol",
    "qwenwork": "qwork-advanced",
}
# 可直接唤醒的端（board-wake.py push 成功即生效）。trae 仅队列-only，排在最后。
WAKE_CAPABLE = {"workbuddy", "antigravity", "codex", "qwenwork"}
WAKE_REVIEW_ORDER = ("workbuddy", "antigravity", "codex", "qwenwork", "trae")
WAKE_QUEUE_FILE = "_wake-queue.jsonl"


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)


def load_tasks(board_root: Path) -> list[tuple[Path, dict]]:
    tasks_root = board_root / "tasks"
    result = []
    if not tasks_root.is_dir():
        return result
    for task_dir in sorted(tasks_root.iterdir()):
        if not is_physical_task_dir(task_dir, tasks_root):
            continue
        try:
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        result.append((task_dir, status))
    return result


def task_sort_key(item: tuple[Path, dict]) -> tuple:
    task_dir, status = item
    priority = PRIORITY_RANK.get(status.get("priority", "normal"), 2)
    created = parse_iso(status.get("created_at") or status.get("created"))
    return priority, created or datetime.max.replace(tzinfo=CN_TZ), task_dir.name


def review_sort_key(item: tuple[Path, dict], now: datetime) -> tuple:
    task_dir, status = item
    started = parse_iso(status.get("status_changed_at") or status.get("claimed_at"))
    age = now - started if started else timedelta(0)
    overdue_rank = 0 if age >= timedelta(hours=2) else 1
    priority = PRIORITY_RANK.get(status.get("priority", "normal"), 2)
    return overdue_rank, priority, started or datetime.max.replace(tzinfo=CN_TZ), task_dir.name


def dependencies_done(status: dict, by_id: dict[str, dict]) -> bool:
    return all(by_id.get(dep, {}).get("status") == "done" for dep in status.get("depends_on", []))


def lifecycle_ready(task_id: str, status: dict, by_id: dict[str, dict]) -> bool:
    if status.get("task_type") == "decision" and not status.get("decision_activated_at"):
        return False
    replaced = status.get("supersedes")
    if replaced:
        parent = by_id.get(replaced, {})
        if parent.get("status") != "superseded" or parent.get("superseded_by") != task_id:
            return False
    return True


def action_payload(
    action: str,
    task_dir: Path | None,
    reason: str,
    command: str | None,
    status: dict | None = None,
) -> dict:
    payload = {
        "action": action,
        "task": task_dir.name if task_dir else None,
        "reason": reason,
        "command": command,
    }
    if task_dir:
        payload["card"] = str(task_dir / "card.md")
    if status:
        payload["status"] = status.get("status")
        payload["complexity"] = status.get("complexity", "L1")
        payload["priority"] = status.get("priority", "normal")
    return payload


def wake_command(task_dir: Path, channel: str, mode: str, model: str) -> str:
    prompt_file = f".kb/board/tasks/{task_dir.name}/card.md"
    return (
        "python3 .kb/board/board-wake.py wake --urgent "
        f"--mode {mode} --channel {channel} --target-model {model} "
        f"--task {task_dir.name} --prompt-file {prompt_file}"
    )


def wake_payload(task_dir: Path, status: dict, channel: str, mode: str, model: str) -> dict:
    payload = action_payload(
        "wake",
        task_dir,
        f"delegate_{mode}_to_{channel}",
        wake_command(task_dir, channel, mode, model),
        status,
    )
    payload.update({"channel": channel, "mode": mode, "target_model": model})
    return payload


def check_wake_queue(board_root: Path, agent_key: str, by_id: dict[str, dict]) -> dict | None:
    """检查共享唤醒队列，返回本端待处理的最新一条 entry。

    队列是 append-only JSONL，本函数只读不写。
    通过任务状态过滤已处理的条目：
    - mode=execute 且 task status=open → 待处理
    - mode=execute 且 task status!=open → 已处理（跳过）
    - mode=review 且 task status=awaiting_review → 待处理
    - mode=review 且 task status!=awaiting_review → 已处理（跳过）
    """
    queue_path = board_root / WAKE_QUEUE_FILE
    if not queue_path.exists():
        return None
    try:
        lines = queue_path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("channel") != agent_key:
            continue
        if entry.get("status") != "pending":
            continue
        task_id = entry.get("task")
        if not task_id:
            continue
        task_status = by_id.get(task_id, {})
        current_status = task_status.get("status")
        mode = entry.get("mode", "execute")
        if mode == "execute" and current_status == "open":
            return entry
        if mode == "review" and current_status == "awaiting_review":
            return entry
    return None


def queue_payload(entry: dict, task_dir: Path | None, status: dict | None) -> dict:
    """构造 execute_from_queue 动作的 payload。"""
    payload = action_payload(
        "execute_from_queue",
        task_dir,
        f"wake_queue_{entry.get('mode', 'execute')}",
        None,
        status,
    )
    payload["prompt_ref"] = entry.get("prompt_ref")
    payload["queue_mode"] = entry.get("mode")
    payload["queue_model"] = entry.get("model")
    return payload


def explicit_executor_route(status: dict, dispatcher: str) -> tuple[str, str] | None:
    """选择执行端，优先可直接唤醒的 agent（trae 排最后，仅队列兜底）。"""
    required = set(status.get("required_caps", []))
    candidates = []
    for target, model in WAKE_TARGET_MODELS.items():
        if target == dispatcher or target not in required:
            continue
        profile = AGENT_CONTRACTS[target]
        if not required.issubset(set(profile["caps"])):
            continue
        if level_value(status.get("complexity", "L1")) > level_value(
            effective_execute_level(target, model)
        ):
            continue
        wake_priority = 0 if target in WAKE_CAPABLE else 1
        candidates.append((wake_priority, target, model))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1], candidates[0][2]


def choose_next_action(
    board_root: Path,
    agent: str,
    model: str,
    now: datetime | None = None,
    *,
    context: str = "interactive",
    current_task: str | None = None,
    current_work_key: str | None = None,
) -> dict:
    agent_key = canonical_agent(agent)
    if not agent_key or not validate_model_label(model):
        raise ValueError("必须提供注册端名和真实模型档名")
    profile = AGENT_CONTRACTS[agent_key]
    current_time = now or now_cn()
    tasks = load_tasks(board_root)
    by_id = {task_dir.name: status for task_dir, status in tasks}
    if context not in {"interactive", "patrol", "maintenance"}:
        raise ValueError(f"未知 execution context: {context!r}")
    if context == "maintenance":
        return action_payload(
            "maintenance",
            None,
            "explicit_maintenance_context",
            "python3 .kb/board/board-maintenance.py --apply",
        )
    if context == "interactive":
        selected = []
        for task_dir, status in tasks:
            if current_task and task_dir.name == current_task:
                selected.append((task_dir, status))
            elif current_work_key and status.get("work_key") == current_work_key:
                selected.append((task_dir, status))
        if not selected:
            return action_payload(
                "idle",
                None,
                "interactive_user_request_in_progress",
                None,
            )
        tasks = selected
    integrity = {
        task_dir.name: get_task_integrity(task_dir, status)
        for task_dir, status in tasks
    }

    reconcile_pending = [
        item for item in tasks if (item[0] / RECONCILE_PENDING_FILE).exists()
    ]
    if reconcile_pending:
        task_dir, status = sorted(reconcile_pending, key=task_sort_key)[0]
        command = (
            "python3 .kb/board/board-reconcile-history.py "
            f"--task {task_dir.name} --recover"
        )
        return action_payload(
            "recover", task_dir, "reconcile_pending", command, status
        )

    pending = [item for item in tasks if (item[0] / ".transition-pending.json").exists()]
    if pending:
        task_dir, status = sorted(pending, key=task_sort_key)[0]
        command = f"python3 .kb/board/board-task-transition.py --task {task_dir.name} --action recover"
        return action_payload("recover", task_dir, "transition_pending", command, status)

    owned = [
        item for item in tasks
        if item[1].get("status") == "claimed"
        and canonical_agent(item[1].get("executor")) == agent_key
        and integrity[item[0].name].schedulable
    ]
    if owned:
        owned.sort(
            key=lambda item: (
                0 if item[1].get("review_result") == "fail" else 1,
                *task_sort_key(item),
            )
        )
        task_dir, status = owned[0]
        reason = "review_failed_resume" if status.get("review_result") == "fail" else "owned_claim_resume"
        return action_payload("resume", task_dir, reason, None, status)

    # 检查共享唤醒队列（他端通过 board-wake.py 委派的任务，v1.5 混合模式）
    queue_entry = check_wake_queue(board_root, agent_key, by_id)
    if queue_entry:
        q_task_id = queue_entry.get("task")
        q_task_dir = board_root / "tasks" / q_task_id if q_task_id else None
        q_status = by_id.get(q_task_id, {})
        return queue_payload(queue_entry, q_task_dir, q_status)

    reviewable = []
    for item in tasks:
        task_dir, status = item
        if status.get("status") != "awaiting_review":
            continue
        if not integrity[item[0].name].schedulable:
            continue
        try:
            card_text = (task_dir / "card.md").read_text(encoding="utf-8")
            validate_reviewer(
                status,
                agent_key,
                model,
                task_dir=task_dir,
                card_text=card_text,
            )
        except (TransitionError, KeyError):
            continue
        except OSError:
            continue
        reviewable.append(item)
    if reviewable:
        task_dir, status = sorted(
            reviewable, key=lambda item: review_sort_key(item, current_time)
        )[0]
        started = parse_iso(status.get("status_changed_at") or status.get("claimed_at"))
        overdue = bool(started and current_time - started >= timedelta(hours=2))
        command = (
            "python3 .kb/board/board-task-transition.py "
            f"--task {task_dir.name} --action review --actor {agent_key} --model {model} "
            "--request-id <unique-id> --result <pass|fail> --issues <N>"
        )
        return action_payload(
            "review", task_dir, "review_overdue" if overdue else "review_ready", command, status
        )

    if agent_key == WAKE_DISPATCHER:
        delegated_reviews = []
        for item in tasks:
            task_dir, status = item
            if status.get("status") != "awaiting_review":
                continue
            if not integrity[task_dir.name].schedulable:
                continue
            try:
                card_text = (task_dir / "card.md").read_text(encoding="utf-8")
            except OSError:
                continue
            routes = eligible_reviewers(
                status,
                status.get("executor"),
                executor_model=status.get("executor_model"),
                card_text=card_text,
                include_authorization=review_authorization_is_event_backed(task_dir, status),
            )
            by_reviewer = {route.get("reviewer"): route for route in routes}
            for reviewer in WAKE_REVIEW_ORDER:
                route = by_reviewer.get(reviewer)
                if not route or reviewer == agent_key:
                    continue
                reviewer_model = route.get("reviewer_model") or WAKE_TARGET_MODELS.get(reviewer)
                if not reviewer_model:
                    continue
                try:
                    validate_reviewer(
                        status,
                        reviewer,
                        reviewer_model,
                        task_dir=task_dir,
                        card_text=card_text,
                    )
                except TransitionError:
                    continue
                delegated_reviews.append((item, reviewer, reviewer_model))
                break
        if delegated_reviews:
            (task_dir, status), reviewer, reviewer_model = sorted(
                delegated_reviews,
                key=lambda candidate: review_sort_key(candidate[0], current_time),
            )[0]
            return wake_payload(task_dir, status, reviewer, "review", reviewer_model)

    claimable = []
    wakeable = []
    reviewer_deadlock_seen = False
    own_caps = set(profile["caps"])
    for item in tasks:
        task_dir, status = item
        if status.get("status") != "open" or status.get("task_type") == "tracking":
            continue
        if not lifecycle_ready(task_dir.name, status, by_id):
            continue
        if not integrity[task_dir.name].schedulable:
            continue
        if not dependencies_done(status, by_id):
            continue
        route_kind = "claim"
        target_agent = agent_key
        target_model = model
        if not set(status.get("required_caps", [])).issubset(own_caps):
            route = explicit_executor_route(status, agent_key) if agent_key == WAKE_DISPATCHER else None
            if route is None:
                continue
            route_kind = "wake"
            target_agent, target_model = route
        if level_value(status.get("complexity", "L1")) > level_value(
            effective_execute_level(target_agent, target_model)
        ):
            continue
        if requires_independent_review(status):
            try:
                card_text = (task_dir / "card.md").read_text(encoding="utf-8")
            except OSError:
                continue
            candidate_status = dict(status)
            candidate_status.update(
                {"executor": target_agent, "executor_model": target_model}
            )
            if not eligible_reviewers(
                candidate_status,
                target_agent,
                executor_model=target_model,
                card_text=card_text,
                include_authorization=review_authorization_is_event_backed(
                    task_dir, status
                ),
            ):
                reviewer_deadlock_seen = True
                continue
        if route_kind == "claim":
            claimable.append(item)
        else:
            wakeable.append((item, target_agent, target_model))
    open_actions = [("claim", item, None, None) for item in claimable]
    open_actions.extend(
        ("wake", item, target_agent, target_model)
        for item, target_agent, target_model in wakeable
    )
    if open_actions:
        kind, (task_dir, status), target_agent, target_model = sorted(
            open_actions,
            key=lambda candidate: task_sort_key(candidate[1]),
        )[0]
        if kind == "wake":
            return wake_payload(task_dir, status, target_agent, "execute", target_model)
        command = (
            "python3 .kb/board/board-task-claim.py "
            f"--task {task_dir.name} --agent {agent_key} --model {model}"
        )
        return action_payload("claim", task_dir, "eligible_open_task", command, status)

    if reviewer_deadlock_seen:
        return action_payload(
            "idle", None, NO_ELIGIBLE_INDEPENDENT_REVIEWER, None
        )
    return action_payload("idle", None, "no_eligible_action", None)


def _pending_callbacks(board_root: Path, agent_key: str) -> list[dict]:
    """返回该端未确认的 review 回调列表（CP9+ 通知机制）。"""
    cb_path = board_root / "callbacks.jsonl"
    if not cb_path.exists():
        return []
    result = []
    for line in cb_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cb = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not cb.get("acknowledged")
            and cb.get("notify_target") == agent_key
            and cb.get("trigger") == "review"
        ):
            result.append({
                "callback_id": cb.get("callback_id"),
                "task_id": cb.get("task_id"),
                "review_result": cb.get("review_result"),
                "reviewer": cb.get("reviewer"),
                "task_status": cb.get("task_status"),
            })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="返回注册端当前唯一最高优先级动作")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--context",
        choices=["interactive", "patrol", "maintenance"],
        default="interactive",
    )
    parser.add_argument("--current-task", default="")
    parser.add_argument("--current-work-key", default="")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    args = parser.parse_args()
    try:
        agent_key = canonical_agent(args.agent)
        payload = choose_next_action(
            Path(args.board_root).resolve(),
            args.agent,
            args.model,
            context=args.context,
            current_task=args.current_task or None,
            current_work_key=args.current_work_key or None,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    # CP9+: 附加待确认回调通知
    if agent_key:
        cbs = _pending_callbacks(Path(args.board_root).resolve(), agent_key)
        if cbs:
            payload["pending_callbacks"] = cbs
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
