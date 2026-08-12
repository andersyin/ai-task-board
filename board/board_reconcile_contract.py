#!/usr/bin/env python3
"""Validation primitives for audited historical reconciliation.

This module is deliberately read-only.  It validates the explicit boundary
between an immutable historical prefix and the first ``reconcile-history``
event, then validates every event after that boundary normally.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from board_transition import (
    TransitionError,
    event_hash,
    status_hash,
    status_projection,
    transition_allowed,
)


RECONCILE_ACTION = "reconcile-history"
RECONCILED_HISTORY_SCOPE = "reconciled"
RECONCILE_ANCHOR_SCHEMA = "board-reconcile-anchor/v1"
RECONCILE_PENDING_FILE = ".reconcile-pending.json"
RECONCILE_PENDING_SCHEMA = "board-reconcile-pending/v1"
RECONCILE_BACKUP_SCHEMA = "board-reconcile-backup/v1"
RECONCILIATION_SCHEMA = "board-history-reconciliation/v1"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def event_file_lines(path: Path) -> list[bytes]:
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise TransitionError("events.jsonl 必须以换行结束，reconcile 不得改写旧前缀")
    lines = raw.splitlines(keepends=True)
    if any(not line.strip() for line in lines):
        raise TransitionError("events.jsonl 含空行")
    return lines


def _legacy_prefix_valid(events: list[dict]) -> tuple[bool, str | None, str]:
    """Validate a pre-reconcile prefix with the legacy/full rules."""
    if not events:
        return False, None, "empty historical prefix"
    first = events[0]
    scope = first.get("history_scope")
    legacy_archive_anchor = first.get("action") == "archive" and not first.get("event_hash")
    if first.get("action") == "create":
        anchor_ok = (
            scope == "full"
            and first.get("from_status") is None
            and first.get("to_status") in {"open", "awaiting_user_approval"}
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
        return False, scope, "invalid create/bootstrap anchor"

    previous = None
    seen_requests: set[str] = set()
    for expected_seq, event in enumerate(events, 1):
        if event.get("schema") != "board-event/v1" or event.get("seq") != expected_seq:
            return False, scope, f"invalid schema/sequence at event {expected_seq}"
        if event.get("state_revision") is not None and event.get("state_revision") != expected_seq:
            return False, scope, f"invalid state revision at event {expected_seq}"
        is_legacy_archive = event.get("action") == "archive" and not event.get("event_hash")
        if not is_legacy_archive and event_hash(event) != event.get("event_hash"):
            return False, scope, f"event hash mismatch at event {expected_seq}"
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in seen_requests:
            return False, scope, f"invalid/duplicate request_id at event {expected_seq}"
        seen_requests.add(request_id)
        if event.get("history_scope") != scope:
            return False, scope, f"history scope drift at event {expected_seq}"
        if previous is None:
            if event.get("prev_event_hash") is not None:
                return False, scope, "first prev_event_hash must be null"
        else:
            if event.get("prev_event_hash") != previous.get("event_hash"):
                return False, scope, f"prev hash break at event {expected_seq}"
            if not is_legacy_archive and event.get("before_hash") != previous.get("after_hash"):
                return False, scope, f"status hash break at event {expected_seq}"
            if event.get("from_status") != previous.get("to_status"):
                return False, scope, f"status chain break at event {expected_seq}"
            if not transition_allowed(
                event.get("action"), event.get("from_status"), event.get("to_status")
            ):
                return False, scope, (
                    f"illegal transition {event.get('from_status')}->{event.get('to_status')}"
                )
        previous = event
    return True, scope, "valid historical prefix"


def _safe_backup_manifest(task_dir: Path, anchor: dict) -> tuple[Path | None, str]:
    reference = anchor.get("backup_manifest")
    expected_hash = anchor.get("backup_manifest_sha256")
    if not isinstance(reference, str) or not reference or not isinstance(expected_hash, str):
        return None, "reconcile anchor missing backup manifest proof"
    board_root = task_dir.parent.parent.resolve()
    candidate = (board_root / reference).resolve()
    backup_root = (board_root / "reconcile-backups").resolve()
    try:
        candidate.relative_to(backup_root)
    except ValueError:
        return None, "backup manifest escapes reconcile-backups"
    if not candidate.is_file() or candidate.is_symlink():
        return None, "backup manifest missing or unsafe"
    if sha256_file(candidate) != expected_hash:
        return None, "backup manifest hash mismatch"
    try:
        manifest = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "backup manifest unreadable"
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != RECONCILE_BACKUP_SCHEMA
        or manifest.get("task_id") != task_dir.name
        or manifest.get("request_id") != anchor.get("request_id")
        or manifest.get("source_files") != anchor.get("expected_files")
    ):
        return None, "backup manifest does not bind anchor proof"
    return candidate, ""


def _safe_proof_file(
    task_dir: Path,
    reference: object,
    expected_hash: object,
    label: str,
) -> str:
    if not isinstance(reference, str) or not reference or not isinstance(expected_hash, str):
        return f"reconcile anchor missing {label} proof"
    board_root = task_dir.parent.parent.resolve()
    proof_root = board_root.parent.resolve()
    candidate = (proof_root / reference).resolve()
    try:
        candidate.relative_to(proof_root)
    except ValueError:
        return f"{label} proof escapes .kb root"
    if not candidate.is_file() or candidate.is_symlink():
        return f"{label} proof missing or unsafe"
    if sha256_file(candidate) != expected_hash:
        return f"{label} proof hash mismatch"
    return ""


def validate_reconciled_ledger(
    task_dir: Path,
    events: list[dict],
    status: dict,
) -> tuple[bool, str, int | None]:
    """Validate a single reconcile anchor and the strict chain after it."""
    anchors = [index for index, event in enumerate(events) if event.get("action") == RECONCILE_ACTION]
    if not anchors:
        return False, "no reconcile-history anchor", None
    if len(anchors) != 1:
        return False, "exactly one reconcile-history anchor is allowed", None
    anchor_index = anchors[0]
    if anchor_index == 0:
        return False, "reconcile anchor cannot erase an empty prefix", anchor_index
    anchor_event = events[anchor_index]
    anchor = anchor_event.get("reconcile_anchor")
    if not isinstance(anchor, dict) or anchor.get("schema") != RECONCILE_ANCHOR_SCHEMA:
        return False, "reconcile anchor payload missing or invalid", anchor_index
    if anchor_event.get("schema") != "board-event/v1" or anchor_event.get("seq") != anchor_index + 1:
        return False, "reconcile event schema/sequence invalid", anchor_index
    if anchor_event.get("state_revision") != anchor_event.get("seq"):
        return False, "reconcile event state revision invalid", anchor_index
    if anchor_event.get("history_scope") != RECONCILED_HISTORY_SCOPE:
        return False, "reconcile event must switch scope to reconciled", anchor_index
    if anchor_event.get("actor") != "user" or anchor_event.get("actor_model") != "human":
        return False, "reconcile-history must be a user/human governance event", anchor_index
    if event_hash(anchor_event) != anchor_event.get("event_hash"):
        return False, "reconcile event hash mismatch", anchor_index
    if anchor.get("task_id") != task_dir.name or anchor.get("request_id") != anchor_event.get("request_id"):
        return False, "reconcile anchor task/request binding mismatch", anchor_index
    if anchor.get("prefix_event_count") != anchor_index:
        return False, "reconcile prefix event count mismatch", anchor_index

    try:
        lines = event_file_lines(task_dir / "events.jsonl")
    except (OSError, TransitionError) as exc:
        return False, str(exc), anchor_index
    if len(lines) != len(events):
        return False, "event byte/JSON count mismatch", anchor_index
    prefix_hash = sha256_bytes(b"".join(lines[:anchor_index]))
    if anchor.get("prefix_sha256") != prefix_hash:
        return False, "reconcile anchor prefix hash mismatch", anchor_index

    required_text = (
        "decision_set_id",
        "decision_set_sha256",
        "decision_ref",
        "adjudication_sha256",
        "evidence_manifest_sha256",
        "evidence_ref",
        "approval_evidence",
        "source_integrity",
        "mode",
    )
    if any(not isinstance(anchor.get(key), str) or not anchor.get(key) for key in required_text):
        return False, "reconcile anchor proof fields incomplete", anchor_index
    if not isinstance(anchor.get("expected_files"), dict):
        return False, "reconcile anchor expected files missing", anchor_index
    _, backup_error = _safe_backup_manifest(task_dir, anchor)
    if backup_error:
        return False, backup_error, anchor_index
    decision_error = _safe_proof_file(
        task_dir,
        anchor.get("decision_ref"),
        anchor.get("decision_set_sha256"),
        "decision set",
    )
    if decision_error:
        return False, decision_error, anchor_index
    evidence_error = _safe_proof_file(
        task_dir,
        anchor.get("evidence_ref"),
        anchor.get("evidence_manifest_sha256"),
        "evidence manifest",
    )
    if evidence_error:
        return False, evidence_error, anchor_index

    prefix = events[:anchor_index]
    prefix_valid, _, _ = _legacy_prefix_valid(prefix)
    previous = prefix[-1]
    mode = anchor.get("mode")
    if prefix_valid:
        if mode != "valid_tail_bridge" or anchor.get("source_integrity") != "history_status_diverged":
            return False, "valid prefix requires history_status_diverged bridge", anchor_index
        if (
            anchor_event.get("prev_event_hash") != previous.get("event_hash")
            or anchor_event.get("before_hash") != previous.get("after_hash")
            or anchor_event.get("from_status") != previous.get("to_status")
        ):
            return False, "reconcile bridge does not continue valid tail", anchor_index
    else:
        if mode != "invalid_prefix_reset" or anchor.get("source_integrity") != "invalid_event_chain":
            return False, "invalid prefix requires explicit invalid_prefix_reset", anchor_index
        if anchor_event.get("prev_event_hash") != previous.get("event_hash"):
            return False, "invalid-prefix anchor does not bind observed tail", anchor_index
        if (
            anchor_event.get("before_hash") != anchor.get("physical_before_status_hash")
            or anchor_event.get("from_status") != anchor.get("physical_before_status")
        ):
            return False, "invalid-prefix anchor physical before binding mismatch", anchor_index

    after_state = anchor_event.get("after_state")
    if not isinstance(after_state, dict) or anchor_event.get("after_hash") != status_hash(after_state):
        return False, "reconcile event after_state/hash mismatch", anchor_index
    if after_state.get("history_reconciliation") != anchor_event.get("history_reconciliation"):
        return False, "reconcile event status audit projection mismatch", anchor_index
    if not transition_allowed(
        RECONCILE_ACTION,
        anchor_event.get("from_status"),
        anchor_event.get("to_status"),
    ):
        return False, "reconcile event target is not allowed", anchor_index

    previous = anchor_event
    seen_requests = {anchor_event.get("request_id")}
    for index in range(anchor_index + 1, len(events)):
        event = events[index]
        expected_seq = index + 1
        if event.get("schema") != "board-event/v1" or event.get("seq") != expected_seq:
            return False, f"post-reconcile schema/sequence invalid at event {expected_seq}", anchor_index
        if event.get("state_revision") is not None and event.get("state_revision") != expected_seq:
            return False, f"post-reconcile state revision invalid at event {expected_seq}", anchor_index
        if event_hash(event) != event.get("event_hash"):
            return False, f"post-reconcile event hash mismatch at event {expected_seq}", anchor_index
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in seen_requests:
            return False, f"post-reconcile request id invalid at event {expected_seq}", anchor_index
        seen_requests.add(request_id)
        if event.get("history_scope") != RECONCILED_HISTORY_SCOPE:
            return False, f"post-reconcile history scope drift at event {expected_seq}", anchor_index
        if (
            event.get("prev_event_hash") != previous.get("event_hash")
            or event.get("before_hash") != previous.get("after_hash")
            or event.get("from_status") != previous.get("to_status")
            or not transition_allowed(event.get("action"), event.get("from_status"), event.get("to_status"))
        ):
            return False, f"post-reconcile chain break at event {expected_seq}", anchor_index
        previous = event

    tail = events[-1]
    if (
        tail.get("to_status") != status.get("status")
        or tail.get("after_hash") != status_hash(status)
        or (
            tail.get("after_state") is not None
            and tail.get("after_state") != status_projection(status)
        )
    ):
        return False, "reconciled ledger tail differs from status.json", anchor_index
    return True, "historical prefix quarantined; ledger valid from audited reconcile anchor", anchor_index
