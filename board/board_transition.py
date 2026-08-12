#!/usr/bin/env python3
"""公告牌状态转换与 append-only 事件账本。

一个任务的所有转换在 task_lock 内串行。跨 status.json / events.jsonl
的一致性使用 pending -> status -> event -> clear pending 前滚协议；
任意一步崩溃后，下一个进程先恢复再处理新请求。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from board_contract import (
    AGENT_CONTRACTS,
    NO_ELIGIBLE_INDEPENDENT_REVIEWER,
    VALID_TRANSITIONS,
    atomic_write_json,
    atomic_write_text,
    canonical_agent,
    eligible_reviewers,
    effective_review_level,
    fsync_directory,
    is_physical_task_dir,
    level_value,
    model_family,
    required_review_level,
    review_authorization_allows,
    task_lock,
    validate_model_label,
    validate_task_id,
)
from board_verdicts import parse_current_verdict


CN_TZ = timezone(timedelta(hours=8))
EVENTS_FILE = "events.jsonl"
PENDING_FILE = ".transition-pending.json"
SYSTEM_REQUEUE_ACTOR = "board-lease-check"
SYSTEM_RECOVERY_ACTOR = "board-transition-recovery"

# 只对状态机投影做 hash；note/card_hash 等展示元数据不纳入。
# reviewer authorization 是正式治理状态，必须纳入 v3 投影与事件链。
STATE_KEYS = (
    "id",
    "contract_version",
    "status",
    "executor",
    "executor_model",
    "claimed_at",
    "lease_expires_at",
    "blocked_at",
    "completed_at",
    "requeue_count",
    "reviewer",
    "reviewer_model",
    "review_result",
    "review_round",
    "review_issues_count",
    "approved_by",
    "approved_at",
    "status_changed_at",
    "review_authorization",
    "history_reconciliation",
)

V3_STATE_KEYS = (
    "work_key",
    "root_work_id",
    "continuation_of",
    "follow_up_of",
    "supersedes",
    "superseded_by",
    "contract_fingerprint",
    "submission_contract_version",
    "submission_contract_fingerprint",
    "submission_outputs",
    "submission_evidence",
    "submitted_at",
    "decision_activated_at",
    "activation_evidence",
    "disposition",
    "cancelled_at",
    "cancellation_reason",
    "superseded_at",
    "archived_at",
    "archived_from",
    "terminal_at",
)

ACTION_TARGETS = {
    "claim": {"claimed"},
    "submit": {"awaiting_review"},
    "block": {"blocked"},
    "review": {"done", "awaiting_user_approval", "claimed"},
    "activate": {"open", "claimed"},  # 实施前批准或失败裁决后恢复
    "approve": {"done"},  # 技术验收后的治理交付终批
    "requeue": {"open", "dlq"},
    "cancel": {"cancelled"},
    "supersede": {"superseded"},
    "create": {"open", "awaiting_user_approval"},  # v1.3: CP2/CP3 genesis
    "archive": {"done", "archived"},  # done 仅兼容旧 archive event；新归档进入 archived
    "authorize-reviewer": {"open", "claimed", "awaiting_review"},
    # 仅由 board-reconcile-history.py 的证明/备份/锁门禁调用。普通 CLI
    # 不暴露该 action；integrity 还会强制验证 reconcile_anchor。
    "reconcile-history": {"done", "cancelled", "superseded", "awaiting_review"},
}


class TransitionError(RuntimeError):
    pass


class TransitionConflict(TransitionError):
    pass


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def status_projection(status: dict) -> dict:
    keys = STATE_KEYS + (V3_STATE_KEYS if int(status.get("contract_version") or 0) >= 3 else ())
    return {key: status.get(key) for key in keys if key in status}


def status_hash(status: dict) -> str:
    return digest(status_projection(status))


def event_hash(event: dict) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    return digest(payload)


def transition_allowed(action: object, from_status: object, to_status: object) -> bool:
    if action == "authorize-reviewer":
        return from_status in {"open", "claimed", "awaiting_review"} and to_status == from_status
    if action == "reconcile-history":
        return (
            from_status in {
                "open", "claimed", "awaiting_review", "awaiting_user_approval",
                "done", "blocked", "dlq", "cancelled", "superseded", "archived",
            }
            and to_status in ACTION_TARGETS["reconcile-history"]
        )
    return (
        to_status in VALID_TRANSITIONS.get(from_status, set())
        and to_status in ACTION_TARGETS.get(str(action), set())
    )


def request_fingerprint(action: str, actor: str, actor_model: str, payload: object) -> str:
    return digest(
        {"action": action, "actor": actor, "actor_model": actor_model, "payload": payload}
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionError(f"{path.name} 不可读: {exc}") from exc
    if not isinstance(value, dict):
        raise TransitionError(f"{path.name} 必须是 JSON object")
    return value


def load_events(task_dir: Path) -> list[dict]:
    path = task_dir / EVENTS_FILE
    if not path.exists():
        return []
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TransitionError(f"events.jsonl 不可读: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise TransitionError(f"events.jsonl 第 {number} 行为空")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TransitionError(f"events.jsonl 第 {number} 行损坏: {exc}") from exc
        if not isinstance(event, dict):
            raise TransitionError(f"events.jsonl 第 {number} 行不是 object")
        events.append(event)
    return events


def append_event(task_dir: Path, event: dict) -> None:
    path = task_dir / EVENTS_FILE
    line = canonical_json(event) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(task_dir)


def _new_event(
    *,
    seq: int,
    action: str,
    actor: str,
    actor_model: str,
    at: str,
    request_id: str,
    request_fp: str,
    from_status: object,
    to_status: object,
    before_hash: object,
    after_hash: object,
    prev_event_hash: object,
    history_scope: str,
    after_state: dict | None = None,
) -> dict:
    event = {
        "schema": "board-event/v1",
        "seq": seq,
        "action": action,
        "actor": actor,
        "actor_model": actor_model,
        "at": at,
        "request_id": request_id,
        "request_fingerprint": request_fp,
        "from_status": from_status,
        "to_status": to_status,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "prev_event_hash": prev_event_hash,
        "history_scope": history_scope,
        "state_revision": seq,
    }
    if after_state is not None:
        event["after_state"] = after_state
    event["event_hash"] = event_hash(event)
    return event


def write_genesis_ledger(task_dir: Path, status: dict, *, actor: str) -> dict:
    """新任务在 staging 目录中写 create genesis，再与任务目录一起 rename。"""
    at = str(status.get("created_at") or now_cn().isoformat(timespec="seconds"))
    request_id = f"create:{status['id']}:{at}"
    fp = request_fingerprint("create", actor, "not-applicable", {"task_id": status["id"]})
    event = _new_event(
        seq=1,
        action="create",
        actor=actor,
        actor_model="not-applicable",
        at=at,
        request_id=request_id,
        request_fp=fp,
        from_status=None,
        to_status=status.get("status", "open"),  # v1.3: CP2/CP3 创建为 awaiting_user_approval
        before_hash=None,
        after_hash=status_hash(status),
        prev_event_hash=None,
        history_scope="full",
        after_state=status_projection(status),
    )
    atomic_write_text(task_dir / EVENTS_FILE, canonical_json(event) + "\n")
    return event


def _ensure_bootstrap(task_dir: Path, status: dict, events: list[dict]) -> list[dict]:
    if events:
        return events
    current_hash = status_hash(status)
    current = status.get("status")
    request_id = f"bootstrap:{status.get('id')}:{current_hash}"
    fp = request_fingerprint(
        "bootstrap", "board-transition-migration", "deterministic-v1", {"hash": current_hash}
    )
    event = _new_event(
        seq=1,
        action="bootstrap",
        actor="board-transition-migration",
        actor_model="deterministic-v1",
        at=now_cn().isoformat(timespec="microseconds"),
        request_id=request_id,
        request_fp=fp,
        from_status=current,
        to_status=current,
        before_hash=current_hash,
        after_hash=current_hash,
        prev_event_hash=None,
        history_scope="partial",
        after_state=status_projection(status),
    )
    append_event(task_dir, event)
    return [event]


def _find_request(events: list[dict], request_id: str) -> dict | None:
    for event in events:
        if event.get("request_id") == request_id:
            return event
    return None


def committed_request(
    task_dir: Path,
    *,
    request_id: str,
    action: str,
    actor: str,
    actor_model: str,
    request_payload: object,
) -> dict | None:
    """查询已提交请求；同 ID 异参数必须报冲突。"""
    existing = _find_request(load_events(task_dir), request_id)
    if not existing:
        return None
    expected = request_fingerprint(action, actor, actor_model, request_payload)
    if existing.get("request_fingerprint") != expected:
        raise TransitionConflict("request_id 重复但参数不同")
    return existing


def recover_pending_locked(task_dir: Path) -> dict | None:
    pending_path = task_dir / PENDING_FILE
    if not pending_path.exists():
        return None
    pending = _read_json(pending_path)
    before = pending.get("before_status")
    after = pending.get("after_status")
    event = pending.get("event")
    if not all(isinstance(value, dict) for value in (before, after, event)):
        raise TransitionError("pending journal 结构不完整")
    if event_hash(event) != event.get("event_hash"):
        raise TransitionError("pending journal 的 event_hash 不匹配")

    before_hash = status_hash(before)
    after_hash = status_hash(after)
    from_status = before.get("status")
    to_status = after.get("status")
    action = event.get("action")
    if (
        pending.get("schema") != "board-transition-pending/v1"
        or before.get("id") != task_dir.name
        or after.get("id") != task_dir.name
        or event.get("schema") != "board-event/v1"
        or event.get("before_hash") != before_hash
        or event.get("after_hash") != after_hash
        or event.get("from_status") != from_status
        or event.get("to_status") != to_status
        or (
            event.get("state_revision") is not None
            and event.get("state_revision") != event.get("seq")
        )
        or (
            event.get("after_state") is not None
            and event.get("after_state") != status_projection(after)
        )
        or not transition_allowed(action, from_status, to_status)
        or not isinstance(event.get("request_id"), str)
        or not event.get("request_id")
    ):
        raise TransitionError("pending journal 的 before/after/event 不自洽")

    events = load_events(task_dir)
    existing = _find_request(events, str(event.get("request_id")))
    if existing:
        if existing.get("event_hash") != event.get("event_hash") or events[-1] != existing:
            raise TransitionConflict("pending 对应事件不是账本唯一尾事件")
        previous = events[-2] if len(events) > 1 else None
    else:
        previous = events[-1] if events else None
    if (
        previous is None
        or event.get("seq") != int(previous.get("seq", 0)) + 1
        or event.get("prev_event_hash") != previous.get("event_hash")
        or event.get("before_hash") != previous.get("after_hash")
        or event.get("history_scope") != previous.get("history_scope")
    ):
        raise TransitionError("pending journal 与 committed event 尾链不自洽")

    status_path = task_dir / "status.json"
    current = _read_json(status_path)
    current_hash = status_hash(current)
    if current_hash == before_hash:
        atomic_write_json(status_path, after)
    elif current_hash != after_hash:
        raise TransitionConflict(
            "pending 恢复发现 status 既不是 before 也不是 after，拒绝覆盖"
        )

    if existing:
        pass
    else:
        append_event(task_dir, event)
    pending_path.unlink()
    fsync_directory(task_dir)
    return event


def _maybe_fail(point: str) -> None:
    if os.environ.get("BOARD_TRANSITION_FAILPOINT") == point:
        raise TransitionError(f"injected crash at {point}")


def commit_transition_locked(
    task_dir: Path,
    *,
    action: str,
    actor: str,
    actor_model: str,
    request_id: str,
    updates: dict,
    request_payload: object,
    expected_status: str | None = None,
    expected_fields: dict | None = None,
) -> tuple[dict, dict, bool]:
    """锁内提交转换。返回 (after_status, event, idempotent)。"""
    recover_pending_locked(task_dir)
    status_path = task_dir / "status.json"
    before = _read_json(status_path)
    if before.get("id") != task_dir.name:
        raise TransitionError("status.id 与任务目录不一致")
    fp = request_fingerprint(action, actor, actor_model, request_payload)
    events = load_events(task_dir)
    existing = _find_request(events, request_id)
    if existing:
        if existing.get("request_fingerprint") != fp:
            raise TransitionConflict("request_id 重复但参数不同")
        return before, existing, True
    if events and events[-1].get("after_hash") != status_hash(before):
        raise TransitionConflict(
            "当前 status 与最后 committed event 不一致，拒绝在坏链上续写"
        )
    if expected_status is not None and before.get("status") != expected_status:
        raise TransitionConflict(
            f"期望 status={expected_status}，实际={before.get('status')}"
        )
    for key, value in (expected_fields or {}).items():
        if before.get(key) != value:
            raise TransitionConflict(f"预期字段 {key} 已变化")

    from_status = before.get("status")
    after = dict(before)
    after.update(updates)
    to_status = after.get("status")
    if not transition_allowed(action, from_status, to_status):
        raise TransitionError(f"非法跳转: {from_status} -> {to_status}")

    # 失败请求不得留下任何事件；legacy bootstrap 只在本次转换通过全部
    # from→to/action 门禁后才写入。
    events = _ensure_bootstrap(task_dir, before, events)
    last = events[-1]
    event = _new_event(
        seq=int(last.get("seq", 0)) + 1,
        action=action,
        actor=actor,
        actor_model=actor_model,
        at=now_cn().isoformat(timespec="microseconds"),
        request_id=request_id,
        request_fp=fp,
        from_status=from_status,
        to_status=to_status,
        before_hash=status_hash(before),
        after_hash=status_hash(after),
        prev_event_hash=last.get("event_hash"),
        history_scope=str(last.get("history_scope") or "partial"),
        after_state=status_projection(after),
    )
    if action == "authorize-reviewer":
        event["review_authorization"] = after.get("review_authorization")
        event["event_hash"] = event_hash(event)
    pending = {"schema": "board-transition-pending/v1", "before_status": before, "after_status": after, "event": event}
    pending_path = task_dir / PENDING_FILE
    atomic_write_json(pending_path, pending)
    _maybe_fail("after_pending")
    atomic_write_json(status_path, after)
    _maybe_fail("after_status")
    append_event(task_dir, event)
    _maybe_fail("after_event")
    pending_path.unlink()
    fsync_directory(task_dir)
    return after, event, False


def recover_task(board_root: Path, task_id: str) -> dict | None:
    task_dir = board_root / "tasks" / task_id
    tasks_root = board_root / "tasks"
    if not validate_task_id(task_id) or not is_physical_task_dir(task_dir, tasks_root):
        raise TransitionError("任务目录不安全或不存在")
    with task_lock(board_root, task_id):
        return recover_pending_locked(task_dir)


def transition_task(
    board_root: Path,
    task_id: str,
    *,
    action: str,
    actor: str,
    actor_model: str,
    request_id: str,
    updates: dict,
    request_payload: object,
    expected_status: str | None = None,
    expected_fields: dict | None = None,
) -> tuple[dict, dict, bool]:
    task_dir = board_root / "tasks" / task_id
    tasks_root = board_root / "tasks"
    if not validate_task_id(task_id) or not is_physical_task_dir(task_dir, tasks_root):
        raise TransitionError("任务目录不安全或不存在")
    with task_lock(board_root, task_id):
        return commit_transition_locked(
            task_dir,
            action=action,
            actor=actor,
            actor_model=actor_model,
            request_id=request_id,
            updates=updates,
            request_payload=request_payload,
            expected_status=expected_status,
            expected_fields=expected_fields,
        )


def canonical_registered_actor(actor: object, model: object) -> str:
    key = canonical_agent(actor)
    if not key or not validate_model_label(model):
        raise TransitionError("操作者必须是注册端 + 真实模型档名")
    return key


_AUTHORIZATION_IMMUTABLE_KEYS = (
    "schema",
    "authorization_id",
    "task_id",
    "reviewer",
    "reviewer_model",
    "reviewer_family",
    "requested_max_level",
    "reason",
    "scope",
    "one_time",
    "approval_evidence",
    "approved_by",
    "approved_at",
)


def review_authorization_is_event_backed(
    task_dir: Path,
    status: dict,
    *,
    events: list[dict] | None = None,
) -> bool:
    authorization = status.get("review_authorization")
    if not isinstance(authorization, dict):
        return False
    try:
        ledger = load_events(task_dir) if events is None else events
    except TransitionError:
        return False
    immutable = {key: authorization.get(key) for key in _AUTHORIZATION_IMMUTABLE_KEYS}
    for event in ledger:
        event_authorization = event.get("review_authorization")
        after_state = event.get("after_state")
        if (
            event.get("action") != "authorize-reviewer"
            or event.get("actor") != "user"
            or event.get("actor_model") != "human"
            or event.get("from_status") != event.get("to_status")
            or not isinstance(event_authorization, dict)
            or not isinstance(after_state, dict)
            or after_state.get("review_authorization") != event_authorization
        ):
            continue
        event_immutable = {
            key: event_authorization.get(key) for key in _AUTHORIZATION_IMMUTABLE_KEYS
        }
        if event_immutable == immutable:
            return True
    return False


def validate_reviewer(
    status: dict,
    reviewer: str,
    reviewer_model: str,
    *,
    task_dir: Path | None = None,
    card_text: str = "",
) -> str:
    executor = canonical_agent(status.get("executor"))
    reviewer = canonical_agent(reviewer)
    if reviewer not in AGENT_CONTRACTS:
        raise TransitionError("验收方必须是注册端")
    complexity = required_review_level(status, card_text)
    max_level = effective_review_level(reviewer, reviewer_model)
    exec_family = model_family(status.get("executor_model"))
    review_family = model_family(reviewer_model)
    if executor == reviewer:
        raise TransitionError("executor 不得 self-review；reviewer 必须是独立端")
    if level_value(complexity) >= 3 and (
        not exec_family or not review_family or exec_family == review_family
    ):
        raise TransitionError(f"{complexity} 必须由不同模型家族验收")

    event_backed = (
        task_dir is None or review_authorization_is_event_backed(task_dir, status)
    )
    authorized = event_backed and review_authorization_allows(
        status,
        reviewer,
        reviewer_model,
        card_text=card_text,
    )
    native_routes = eligible_reviewers(
        status,
        executor,
        executor_model=status.get("executor_model"),
        card_text=card_text,
        include_authorization=False,
    )
    reviewer_has_native_route = any(
        route.get("reviewer") == reviewer and route.get("route") == "native"
        for route in native_routes
    )
    if not reviewer_has_native_route:
        if authorized:
            return "authorized"
        if not native_routes:
            raise TransitionError(
                f"{NO_ELIGIBLE_INDEPENDENT_REVIEWER}: task={status.get('id')} "
                f"complexity={complexity} executor={executor}"
            )
        raise TransitionError(f"{reviewer}/{reviewer_model} 不满足该任务 reviewer policy")
    if level_value(complexity) > level_value(max_level):
        if authorized:
            return "authorized"
        raise TransitionError(
            f"{reviewer}/{reviewer_model} 最高验收 {max_level}，不能验收 {complexity}"
        )
    return "native"


def signed_progress_exists(task_dir: Path, actor: str, actor_model: str) -> bool:
    path = task_dir / "PROGRESS.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return f"[{actor}/{actor_model}]" in text or f"执行端：{actor}/{actor_model}" in text


def signed_blocked_exists(task_dir: Path, actor: str, actor_model: str) -> bool:
    path = task_dir / "BLOCKED.md"
    if not path.exists():
        return False
    prefix = "\n".join(path.read_text(encoding="utf-8").splitlines()[:3])
    return f"执行端：{actor}/{actor_model}" in prefix


def signed_review_matches(
    task_dir: Path,
    reviewer: str,
    reviewer_model: str,
    result: str,
) -> bool:
    """REVIEW.md 必须绑定本次验收身份和结论，不能复用旧报告。"""
    path = task_dir / "REVIEW.md"
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    identity = re.search(
        rf"^\s*验收端\s*[:：]\s*{re.escape(reviewer)}/{re.escape(reviewer_model)}\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    # 收敛改造：取消 conditional_pass，验收只保留 PASS / FAIL
    verdict = parse_current_verdict(text)
    return bool(identity and verdict == result.lower())
