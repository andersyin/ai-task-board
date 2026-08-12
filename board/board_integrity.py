#!/usr/bin/env python3
"""Pure, read-only integrity classification for one board task.

This module is the consumption gate shared by selectors, direct claim and
projections.  It never repairs, recovers or writes production state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from board_contract import (
    ALLOWED_STATUSES,
    CONTRACT_VERSION,
    VALID_TRANSITIONS,
    card_hash,
    is_physical_task_dir,
)
from board_transition import (
    EVENTS_FILE,
    PENDING_FILE,
    TransitionError,
    event_hash,
    load_events,
    review_authorization_is_event_backed,
    status_hash,
    status_projection,
    transition_allowed,
)
from board_task_contract import load_and_validate_contract
from board_reconcile_contract import (
    RECONCILE_ACTION,
    RECONCILE_PENDING_FILE,
    validate_reconciled_ledger,
)


HEALTHY = "healthy"
LEGACY_UNMANAGED = "legacy_unmanaged"
PARTIAL_HISTORY = "partial_history"
RECONCILED_HISTORY = "reconciled_history"
HISTORY_STATUS_DIVERGED = "history_status_diverged"
INVALID_EVENT_CHAIN = "invalid_event_chain"
INVALID_CONTRACT = "invalid_contract"
UNKNOWN = "unknown"

SCHEDULABLE_INTEGRITY = frozenset(
    {HEALTHY, LEGACY_UNMANAGED, PARTIAL_HISTORY, RECONCILED_HISTORY}
)


@dataclass(frozen=True)
class TaskIntegrity:
    status: str
    detail: str = ""
    history_scope: str | None = None

    @property
    def schedulable(self) -> bool:
        return self.status in SCHEDULABLE_INTEGRITY

    @property
    def quarantined(self) -> bool:
        return not self.schedulable

    def to_dict(self) -> dict:
        value = asdict(self)
        value.update({"schedulable": self.schedulable, "quarantined": self.quarantined})
        return value


def _result(status: str, detail: str = "", scope: str | None = None) -> TaskIntegrity:
    return TaskIntegrity(status=status, detail=detail, history_scope=scope)


def _load_status(task_dir: Path) -> tuple[dict | None, TaskIntegrity | None]:
    status_path = task_dir / "status.json"
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _result(INVALID_CONTRACT, f"status.json unreadable: {exc}")
    if not isinstance(value, dict):
        return None, _result(INVALID_CONTRACT, "status.json must be an object")
    return value, None


def _basic_contract_error(task_dir: Path, status: dict) -> str | None:
    tasks_root = task_dir.parent
    if not is_physical_task_dir(task_dir, tasks_root):
        return "task is not a physical direct child of tasks/"
    if status.get("id") not in {None, task_dir.name}:
        return f"status.id={status.get('id')!r} does not match directory"
    if status.get("status") not in ALLOWED_STATUSES:
        return f"unknown status {status.get('status')!r}"
    version = status.get("contract_version")
    if version is not None and (not isinstance(version, int) or version < 1):
        return f"invalid contract_version {version!r}"
    if isinstance(version, int) and version > CONTRACT_VERSION:
        return f"unsupported future contract_version {version}"
    required_caps = status.get("required_caps")
    if not isinstance(required_caps, list) or not required_caps:
        return "required_caps must be a non-empty list"
    card_path = task_dir / "card.md"
    if not card_path.is_file():
        return "card.md missing"
    stored_hash = status.get("card_hash")
    actual_hash = card_hash(card_path)
    if stored_hash and stored_hash != actual_hash:
        return "card_hash mismatch"
    if (version or 0) >= 2 and not stored_hash:
        return "current card_hash missing"
    return None


def get_task_integrity(task_dir: Path, status: dict | None = None) -> TaskIntegrity:
    """Classify one task without mutating it.

    Compatibility policy is intentionally explicit: readable tasks without an
    event ledger, and valid bootstrap/partial ledgers, remain schedulable.
    Corrupt contracts, broken chains and event/status divergence fail closed.
    """

    task_dir = Path(task_dir)
    if status is None:
        status, error = _load_status(task_dir)
        if error:
            return error
    if not isinstance(status, dict):
        return _result(INVALID_CONTRACT, "status must be an object")

    contract_error = _basic_contract_error(task_dir, status)
    if contract_error:
        return _result(INVALID_CONTRACT, contract_error)
    _, qualified_error = load_and_validate_contract(task_dir, status)
    if qualified_error:
        return _result(INVALID_CONTRACT, qualified_error)

    if (task_dir / PENDING_FILE).exists():
        return _result(UNKNOWN, "transition pending journal requires recovery")
    if (task_dir / RECONCILE_PENDING_FILE).exists():
        return _result(UNKNOWN, "reconcile pending journal requires recovery")

    events_path = task_dir / EVENTS_FILE
    if not events_path.exists():
        if status.get("review_authorization") is not None:
            return _result(
                INVALID_EVENT_CHAIN,
                "review authorization has no formal event ledger",
            )
        return _result(LEGACY_UNMANAGED, "no event ledger; pre-ledger history is untraceable")

    try:
        events = load_events(task_dir)
    except TransitionError as exc:
        return _result(INVALID_EVENT_CHAIN, str(exc))
    if not events:
        return _result(INVALID_EVENT_CHAIN, "events.jsonl exists but is empty")

    if any(event.get("action") == RECONCILE_ACTION for event in events):
        valid, detail, _ = validate_reconciled_ledger(task_dir, events, status)
        if not valid:
            return _result(INVALID_EVENT_CHAIN, detail, "reconciled")
        if status.get("review_authorization") is not None and not review_authorization_is_event_backed(
            task_dir, status, events=events
        ):
            return _result(
                INVALID_EVENT_CHAIN,
                "review authorization lacks matching authorize-reviewer governance event",
                "reconciled",
            )
        return _result(RECONCILED_HISTORY, detail, "reconciled")

    first = events[0]
    scope = first.get("history_scope")
    legacy_archive_anchor = first.get("action") == "archive" and not first.get("event_hash")
    if first.get("action") == "create":
        anchor_ok = (
            scope == "full"
            and first.get("from_status") is None
            and first.get("to_status") in VALID_TRANSITIONS.get(None, set())
            and first.get("before_hash") is None
        )
    elif first.get("action") == "bootstrap":
        anchor_ok = (
            scope == "partial"
            and first.get("from_status") == first.get("to_status")
            and first.get("before_hash") == first.get("after_hash")
        )
    elif legacy_archive_anchor:
        anchor_ok = (
            scope == "full"
            and first.get("from_status") in {None, "open"}
            and first.get("to_status") == "done"
            and first.get("prev_event_hash") is None
        )
    else:
        anchor_ok = False
    if not anchor_ok:
        return _result(INVALID_EVENT_CHAIN, "invalid create/bootstrap anchor", scope)

    previous = None
    seen_requests: set[str] = set()
    has_legacy_archive = False
    for expected_seq, event in enumerate(events, 1):
        if event.get("schema") != "board-event/v1" or event.get("seq") != expected_seq:
            return _result(INVALID_EVENT_CHAIN, f"invalid schema/sequence at event {expected_seq}", scope)
        if event.get("state_revision") is not None and event.get("state_revision") != expected_seq:
            return _result(INVALID_EVENT_CHAIN, f"invalid state revision at event {expected_seq}", scope)
        is_legacy_archive = event.get("action") == "archive" and not event.get("event_hash")
        has_legacy_archive = has_legacy_archive or is_legacy_archive
        if not is_legacy_archive and event_hash(event) != event.get("event_hash"):
            return _result(INVALID_EVENT_CHAIN, f"event hash mismatch at event {expected_seq}", scope)
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in seen_requests:
            return _result(INVALID_EVENT_CHAIN, f"invalid/duplicate request_id at event {expected_seq}", scope)
        seen_requests.add(request_id)
        if event.get("history_scope") != scope:
            return _result(INVALID_EVENT_CHAIN, f"history scope drift at event {expected_seq}", scope)
        if previous is None:
            if event.get("prev_event_hash") is not None:
                return _result(INVALID_EVENT_CHAIN, "first prev_event_hash must be null", scope)
        else:
            if event.get("prev_event_hash") != previous.get("event_hash"):
                return _result(INVALID_EVENT_CHAIN, f"prev hash break at event {expected_seq}", scope)
            if not is_legacy_archive and event.get("before_hash") != previous.get("after_hash"):
                return _result(INVALID_EVENT_CHAIN, f"status hash break at event {expected_seq}", scope)
            if event.get("from_status") != previous.get("to_status"):
                return _result(INVALID_EVENT_CHAIN, f"status chain break at event {expected_seq}", scope)
            from_status = event.get("from_status")
            to_status = event.get("to_status")
            if not transition_allowed(event.get("action"), from_status, to_status):
                return _result(INVALID_EVENT_CHAIN, f"illegal transition {from_status}->{to_status}", scope)
        previous = event

    if status.get("review_authorization") is not None and not review_authorization_is_event_backed(
        task_dir, status, events=events
    ):
        return _result(
            INVALID_EVENT_CHAIN,
            "review authorization lacks matching authorize-reviewer governance event",
            scope,
        )

    tail = events[-1]
    if tail.get("to_status") != status.get("status"):
        return _result(
            HISTORY_STATUS_DIVERGED,
            f"event tail status={tail.get('to_status')!r}, status.json={status.get('status')!r}",
            scope,
        )
    if not has_legacy_archive and tail.get("after_hash") != status_hash(status):
        return _result(HISTORY_STATUS_DIVERGED, "event tail hash differs from status.json", scope)
    if tail.get("after_state") is not None and tail.get("after_state") != status_projection(status):
        return _result(HISTORY_STATUS_DIVERGED, "event tail snapshot differs from status.json", scope)

    if scope == "partial" or has_legacy_archive:
        return _result(PARTIAL_HISTORY, "history is provable only from compatibility anchor", scope)
    if scope == "full":
        return _result(HEALTHY, "full event ledger matches current status", scope)
    return _result(UNKNOWN, f"unknown history scope {scope!r}", scope)


def integrity_allows_consumption(task_dir: Path, status: dict | None = None) -> bool:
    return get_task_integrity(task_dir, status).schedulable
