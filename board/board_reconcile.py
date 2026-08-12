#!/usr/bin/env python3
"""Audited, single-task historical reconciliation transaction.

The normal transition engine intentionally refuses divergent or broken event
history.  This module is the only exception path: it requires a completed user
decision set, exact source hashes, a verified byte-for-byte backup and a
user/human governance invocation.  It never rewrites old event bytes.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from board_contract import (
    ALLOWED_STATUSES,
    atomic_write_json,
    fsync_directory,
    is_physical_task_dir,
    task_lock,
    validate_task_id,
)
from board_reconcile_contract import (
    RECONCILE_ACTION,
    RECONCILE_ANCHOR_SCHEMA,
    RECONCILE_BACKUP_SCHEMA,
    RECONCILE_PENDING_FILE,
    RECONCILE_PENDING_SCHEMA,
    RECONCILED_HISTORY_SCOPE,
    RECONCILIATION_SCHEMA,
    event_file_lines,
    sha256_bytes,
    sha256_file,
)
from board_transition import (
    EVENTS_FILE,
    PENDING_FILE,
    TransitionConflict,
    TransitionError,
    append_event,
    canonical_json,
    digest,
    event_hash,
    load_events,
    now_cn,
    request_fingerprint,
    status_hash,
    status_projection,
)


PROTECTED_FILES = ("status.json", "events.jsonl", "card.md", "PROGRESS.md", "REVIEW.md")
DECISION_SCHEMA = "task-flow-adjudications/v1"
EVIDENCE_SCHEMA = "board-evidence-bundle/v1"
ALLOWED_SOURCE_INTEGRITY = {"history_status_diverged", "invalid_event_chain"}
DECISION_TARGETS = {
    "cancel": "cancelled",
    "supersede": "superseded",
    "keep_current_status": "done",
    "adopt_event_tail": "awaiting_review",
}


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionError(f"{label} 不可读: {exc}") from exc
    if not isinstance(value, dict):
        raise TransitionError(f"{label} 必须是 JSON object")
    return value


def _file_fact(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    if not path.is_file() or path.is_symlink():
        raise TransitionError(f"受保护路径不是安全普通文件: {path.name}")
    data = path.read_bytes()
    return {"exists": True, "bytes": len(data), "sha256": sha256_bytes(data)}


def _pretty_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _relative_file(board_root: Path, path: Path, label: str) -> tuple[Path, str]:
    resolved = path.resolve()
    allowed_root = board_root.parent.resolve()
    try:
        relative = resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise TransitionError(f"{label} 必须位于 KB/.kb 范围内") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise TransitionError(f"{label} 不存在或不是安全普通文件")
    return resolved, relative.as_posix()


def _expected_files(manifest: dict) -> dict[str, dict]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise TransitionError("evidence manifest.files 缺失")
    expected: dict[str, dict] = {}
    for name in PROTECTED_FILES:
        item = files.get(name)
        if item is None:
            expected[name] = {"exists": False}
            continue
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("bytes"), int)
            or not isinstance(item.get("sha256"), str)
        ):
            raise TransitionError(f"evidence manifest {name} hash 结构无效")
        expected[name] = {
            "exists": True,
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }
    if not all(expected[name]["exists"] for name in ("status.json", "events.jsonl", "card.md")):
        raise TransitionError("evidence manifest 必须包含 status/events/card")
    return expected


def _assert_expected_files(task_dir: Path, expected: dict[str, dict]) -> None:
    for name in PROTECTED_FILES:
        actual = _file_fact(task_dir / name)
        if actual != expected.get(name):
            raise TransitionConflict(
                f"EXPECTED_HASH_MISMATCH: {task_dir.name}/{name} "
                f"expected={expected.get(name)} actual={actual}"
            )


def _decision_context(
    board_root: Path,
    task_id: str,
    decision_file: Path,
    decision_set_id: str,
    evidence_manifest: Path,
) -> dict:
    decision_path, decision_ref = _relative_file(board_root, decision_file, "decision file")
    evidence_path, evidence_ref = _relative_file(
        board_root, evidence_manifest, "evidence manifest"
    )
    decisions = _read_json(decision_path, "decision file")
    manifest = _read_json(evidence_path, "evidence manifest")
    if decisions.get("schema") != DECISION_SCHEMA:
        raise TransitionError("decision file schema 不受支持")
    if decisions.get("decision_set_id") != decision_set_id:
        raise TransitionConflict("decision_set_id 与冻结决策文件不一致")
    if decisions.get("authorized_by") != "user" or decisions.get("apply_scope") != "decision_only":
        raise TransitionError("决策集缺少用户授权或不是冻结 decision_only 产物")
    decision_task_id = decisions.get("task_id")
    if not isinstance(decision_task_id, str) or not validate_task_id(decision_task_id):
        raise TransitionError("decision task_id 无效")
    decision_status = _read_json(
        board_root / "tasks" / decision_task_id / "status.json", "decision task status"
    )
    decision_task_dir = board_root / "tasks" / decision_task_id
    from board_integrity import get_task_integrity  # local import avoids module cycle

    decision_integrity = get_task_integrity(decision_task_dir, decision_status)
    if (
        decision_status.get("id") != decision_task_id
        or decision_status.get("status") != "done"
        or decision_status.get("review_result") != "pass"
        or decision_status.get("approved_by") != "user"
        or not decision_integrity.schedulable
    ):
        raise TransitionError("决策任务必须完整性可调度、已独立 PASS 并由用户终批 done")
    decision_events = load_events(decision_task_dir)
    if (
        not decision_events
        or decision_events[-1].get("action") != "approve"
        or decision_events[-1].get("actor") != "user"
        or decision_events[-1].get("actor_model") != "human"
        or not any(
            event.get("action") == "review"
            and isinstance(event.get("after_state"), dict)
            and event["after_state"].get("review_result") == "pass"
            and event.get("actor") != decision_status.get("executor")
            for event in decision_events
        )
    ):
        raise TransitionError("决策任务缺少 event-backed 独立 PASS 与 user approve")

    rows = decisions.get("adjudications")
    if not isinstance(rows, list):
        raise TransitionError("decision adjudications 必须是数组")
    matches = [row for row in rows if isinstance(row, dict) and row.get("task_id") == task_id]
    if len(matches) != 1:
        raise TransitionConflict("决策集必须对目标 task 恰有一条裁决")
    row = matches[0]
    decision = row.get("decision")
    target_status = row.get("target_status")
    if decision not in DECISION_TARGETS or target_status != DECISION_TARGETS[decision]:
        raise TransitionError("裁决 decision/target_status 不在受支持映射")
    if target_status not in ALLOWED_STATUSES:
        raise TransitionError("裁决目标状态无效")
    if not isinstance(row.get("reason"), str) or not row.get("reason").strip():
        raise TransitionError("裁决必须包含 reason")
    if decision == "supersede":
        replacement = row.get("replacement_task")
        if not isinstance(replacement, str) or not validate_task_id(replacement):
            raise TransitionError("supersede 裁决缺 replacement_task")
        replacement_status = _read_json(
            board_root / "tasks" / replacement / "status.json", "replacement task status"
        )
        if replacement_status.get("id") != replacement:
            raise TransitionError("replacement task identity mismatch")

    if (
        manifest.get("schema") != EVIDENCE_SCHEMA
        or manifest.get("task_id") != task_id
        or manifest.get("source_task") != f"tasks/{task_id}"
        or manifest.get("decision_state") != "needs_human_decision"
    ):
        raise TransitionConflict("evidence manifest 未精确绑定目标 task/决策状态")
    expected = _expected_files(manifest)
    return {
        "decisions": decisions,
        "row": row,
        "manifest": manifest,
        "expected_files": expected,
        "decision_ref": decision_ref,
        "evidence_ref": evidence_ref,
        "decision_sha256": sha256_file(decision_path),
        "evidence_sha256": sha256_file(evidence_path),
        "adjudication_sha256": digest(row),
        "decision_task_id": decision_task_id,
    }


def _request_payload(
    task_id: str,
    decision_set_id: str,
    context: dict,
    approval_evidence: str,
) -> dict:
    return {
        "task_id": task_id,
        "decision_set_id": decision_set_id,
        "decision_set_sha256": context["decision_sha256"],
        "adjudication_sha256": context["adjudication_sha256"],
        "evidence_manifest_sha256": context["evidence_sha256"],
        "decision": context["row"].get("decision"),
        "target_status": context["row"].get("target_status"),
        "replacement_task": context["row"].get("replacement_task"),
        "approval_evidence": approval_evidence,
    }


def _backup_dir(board_root: Path, task_id: str, request_id: str) -> Path:
    token = digest({"task_id": task_id, "request_id": request_id})[:20]
    return board_root / "reconcile-backups" / task_id / token


def _verify_backup(directory: Path, expected: dict, request_id: str, request_fp: str) -> dict:
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path, "backup manifest")
    if (
        manifest.get("schema") != RECONCILE_BACKUP_SCHEMA
        or manifest.get("task_id") != directory.parent.name
        or manifest.get("request_id") != request_id
        or manifest.get("request_fingerprint") != request_fp
        or manifest.get("source_files") != expected
    ):
        raise TransitionConflict("已存在 backup 与本次请求不一致")
    copies = manifest.get("copies")
    if not isinstance(copies, dict):
        raise TransitionError("backup copies 清单缺失")
    for name, fact in expected.items():
        copy_path = directory / "files" / name
        actual = _file_fact(copy_path)
        if fact.get("exists"):
            if actual != fact or copies.get(name) != fact:
                raise TransitionError(f"backup byte verification failed: {name}")
        elif copy_path.exists() or copies.get(name) != {"exists": False}:
            raise TransitionError(f"backup absent marker mismatch: {name}")
    return manifest


def _ensure_backup(
    board_root: Path,
    task_dir: Path,
    expected: dict,
    request_id: str,
    request_fp: str,
    proof: dict,
) -> tuple[str, str]:
    final = _backup_dir(board_root, task_dir.name, request_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        _verify_backup(final, expected, request_id, request_fp)
        reference = final.relative_to(board_root).as_posix() + "/manifest.json"
        return reference, sha256_file(final / "manifest.json")

    staging = final.parent / f".creating-{final.name}-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        files_dir = staging / "files"
        files_dir.mkdir()
        copies: dict[str, dict] = {}
        for name, fact in expected.items():
            if fact.get("exists"):
                source = task_dir / name
                target = files_dir / name
                shutil.copyfile(source, target)
                with target.open("rb") as handle:
                    os.fsync(handle.fileno())
                copied = _file_fact(target)
                if copied != fact:
                    raise TransitionError(f"backup copy mismatch: {name}")
                copies[name] = copied
            else:
                copies[name] = {"exists": False}
        manifest = {
            "schema": RECONCILE_BACKUP_SCHEMA,
            "task_id": task_dir.name,
            "request_id": request_id,
            "request_fingerprint": request_fp,
            "created_at": now_cn().isoformat(timespec="microseconds"),
            "source_files": expected,
            "copies": copies,
            "proof": proof,
        }
        atomic_write_json(staging / "manifest.json", manifest)
        fsync_directory(files_dir)
        fsync_directory(staging)
        os.replace(staging, final)
        fsync_directory(final.parent)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    _verify_backup(final, expected, request_id, request_fp)
    reference = final.relative_to(board_root).as_posix() + "/manifest.json"
    return reference, sha256_file(final / "manifest.json")


def _maybe_fail(point: str) -> None:
    if os.environ.get("BOARD_RECONCILE_FAILPOINT") == point:
        raise TransitionError(f"injected reconcile crash at {point}")


def _after_status(before: dict, row: dict, at: str, reconciliation: dict) -> dict:
    after = dict(before)
    decision = row["decision"]
    target = row["target_status"]
    if decision == "keep_current_status" and before.get("status") != target:
        raise TransitionConflict("keep_current_status target 与磁盘当前状态不一致")
    after.update(
        {
            "status": target,
            "status_changed_at": at,
            "history_reconciliation": reconciliation,
        }
    )
    if decision == "cancel":
        after.update(
            {
                "disposition": "cancelled",
                "cancelled_at": at,
                "cancellation_reason": row["reason"],
                "terminal_at": at,
                "lease_expires_at": None,
                "completed_at": None,
            }
        )
    elif decision == "supersede":
        after.update(
            {
                "disposition": "superseded",
                "superseded_by": row["replacement_task"],
                "superseded_at": at,
                "terminal_at": at,
                "lease_expires_at": None,
                "completed_at": None,
            }
        )
    elif decision == "adopt_event_tail":
        after.update(
            {
                "lease_expires_at": None,
                "completed_at": None,
                "reviewer": None,
                "reviewer_model": None,
                "review_result": None,
                "review_round": 0,
                "review_issues_count": None,
            }
        )
    return after


def _backup_manifest_from_reference(board_root: Path, reference: str) -> tuple[Path, Path]:
    manifest_path = (board_root / reference).resolve()
    backup_root = (board_root / "reconcile-backups").resolve()
    try:
        manifest_path.relative_to(backup_root)
    except ValueError as exc:
        raise TransitionError("pending backup reference escapes reconcile-backups") from exc
    return manifest_path, manifest_path.parent


def recover_reconcile_pending_locked(board_root: Path, task_dir: Path) -> dict | None:
    pending_path = task_dir / RECONCILE_PENDING_FILE
    if not pending_path.exists():
        return None
    pending = _read_json(pending_path, "reconcile pending")
    before = pending.get("physical_before_status")
    after = pending.get("after_status")
    event = pending.get("event")
    expected = pending.get("expected_files")
    if (
        pending.get("schema") != RECONCILE_PENDING_SCHEMA
        or pending.get("task_id") != task_dir.name
        or not all(isinstance(value, dict) for value in (before, after, event, expected))
        or event.get("action") != RECONCILE_ACTION
        or event_hash(event) != event.get("event_hash")
        or event.get("after_hash") != status_hash(after)
        or event.get("after_state") != status_projection(after)
    ):
        raise TransitionError("reconcile pending journal 结构或 hash 不自洽")
    backup_reference = pending.get("backup_manifest")
    backup_hash = pending.get("backup_manifest_sha256")
    if not isinstance(backup_reference, str) or not isinstance(backup_hash, str):
        raise TransitionError("reconcile pending 缺 backup proof")
    backup_manifest_path, backup_dir = _backup_manifest_from_reference(
        board_root, backup_reference
    )
    if sha256_file(backup_manifest_path) != backup_hash:
        raise TransitionError("reconcile pending backup manifest hash mismatch")
    _verify_backup(
        backup_dir,
        expected,
        str(event.get("request_id")),
        str(event.get("request_fingerprint")),
    )

    for name in ("card.md", "PROGRESS.md", "REVIEW.md"):
        if _file_fact(task_dir / name) != expected.get(name):
            raise TransitionConflict(f"reconcile recovery detected drift: {name}")

    before_status_bytes = (backup_dir / "files" / "status.json").read_bytes()
    before_events_bytes = (backup_dir / "files" / "events.jsonl").read_bytes()
    after_status_bytes = _pretty_json_bytes(after)
    after_events_bytes = before_events_bytes + (canonical_json(event) + "\n").encode("utf-8")

    status_path = task_dir / "status.json"
    events_path = task_dir / EVENTS_FILE
    current_status_bytes = status_path.read_bytes()
    current_events_bytes = events_path.read_bytes()
    if current_status_bytes == before_status_bytes:
        atomic_write_json(status_path, after)
    elif current_status_bytes != after_status_bytes:
        raise TransitionConflict("reconcile recovery status is neither before nor after")
    if current_events_bytes == before_events_bytes:
        append_event(task_dir, event)
    elif current_events_bytes != after_events_bytes:
        raise TransitionConflict("reconcile recovery events are neither before nor after")

    pending_path.unlink()
    fsync_directory(task_dir)
    return event


def recover_reconcile_task(board_root: Path, task_id: str) -> dict | None:
    task_dir = board_root / "tasks" / task_id
    if not validate_task_id(task_id) or not is_physical_task_dir(task_dir, board_root / "tasks"):
        raise TransitionError("任务目录不安全或不存在")
    with task_lock(board_root, task_id):
        return recover_reconcile_pending_locked(board_root, task_dir)


def reconcile_history(
    board_root: Path,
    task_id: str,
    *,
    decision_file: Path,
    decision_set_id: str,
    evidence_manifest: Path,
    actor: str,
    actor_model: str,
    request_id: str,
    approval_evidence: str,
    apply: bool,
) -> dict:
    board_root = Path(board_root).resolve()
    task_dir = board_root / "tasks" / task_id
    if not validate_task_id(task_id) or not is_physical_task_dir(task_dir, board_root / "tasks"):
        raise TransitionError("任务目录不安全或不存在")
    if actor.strip().lower() != "user" or actor_model != "human":
        raise TransitionError("reconcile-history 只能由 actor=user/model=human 治理调用")
    if not isinstance(request_id, str) or not request_id.strip():
        raise TransitionError("reconcile-history 必须提供唯一 request-id")
    if not isinstance(approval_evidence, str) or not approval_evidence.strip():
        raise TransitionError("reconcile-history 必须提供用户 approval evidence")

    context = _decision_context(
        board_root, task_id, Path(decision_file), decision_set_id, Path(evidence_manifest)
    )
    payload = _request_payload(task_id, decision_set_id, context, approval_evidence)
    request_fp = request_fingerprint(
        RECONCILE_ACTION, "user", "human", payload
    )

    with task_lock(board_root, task_id):
        if (task_dir / PENDING_FILE).exists():
            raise TransitionError("普通 transition pending 未恢复，reconcile 拒绝并发处理")
        if apply:
            recover_reconcile_pending_locked(board_root, task_dir)
        elif (task_dir / RECONCILE_PENDING_FILE).exists():
            raise TransitionError("reconcile pending 存在；dry-run 不得隐式恢复")

        events = load_events(task_dir)
        existing = next(
            (event for event in events if event.get("request_id") == request_id), None
        )
        if existing:
            if (
                existing.get("action") != RECONCILE_ACTION
                or existing.get("request_fingerprint") != request_fp
            ):
                raise TransitionConflict("request_id 重复但参数不同")
            status = _read_json(task_dir / "status.json", "status.json")
            return {
                "ok": True,
                "applied": True,
                "idempotent": True,
                "task_id": task_id,
                "status": status.get("status"),
                "event_seq": existing.get("seq"),
                "request_id": request_id,
            }
        if any(event.get("action") == RECONCILE_ACTION for event in events):
            raise TransitionConflict("该 task 已消费历史 reconcile；不得复用决策集二次改写")

        from board_integrity import get_task_integrity  # local import avoids module cycle

        before = _read_json(task_dir / "status.json", "status.json")
        integrity = get_task_integrity(task_dir, before)
        if integrity.status not in ALLOWED_SOURCE_INTEGRITY:
            raise TransitionError(
                f"reconcile-history 只消费 quarantined history，当前={integrity.status}"
            )
        _assert_expected_files(task_dir, context["expected_files"])
        if not events:
            raise TransitionError("reconcile-history 要求保留非空历史前缀")

        row = context["row"]
        last = events[-1]
        if row["decision"] == "adopt_event_tail":
            if integrity.status != "history_status_diverged":
                raise TransitionError("invalid event chain 不得 adopt_event_tail")
            if last.get("to_status") != row["target_status"]:
                raise TransitionConflict("adopt_event_tail target 与可信 event tail 不一致")

        mode = (
            "valid_tail_bridge"
            if integrity.status == "history_status_diverged"
            else "invalid_prefix_reset"
        )
        proof = {
            "decision_set_id": decision_set_id,
            "decision_set_sha256": context["decision_sha256"],
            "adjudication_sha256": context["adjudication_sha256"],
            "evidence_manifest_sha256": context["evidence_sha256"],
            "decision_ref": context["decision_ref"],
            "evidence_ref": context["evidence_ref"],
        }
        if not apply:
            return {
                "ok": True,
                "applied": False,
                "dry_run": True,
                "task_id": task_id,
                "source_integrity": integrity.status,
                "mode": mode,
                "decision": row["decision"],
                "target_status": row["target_status"],
                "expected_files": context["expected_files"],
                "request_fingerprint": request_fp,
            }

        backup_reference, backup_hash = _ensure_backup(
            board_root,
            task_dir,
            context["expected_files"],
            request_id,
            request_fp,
            proof,
        )
        _maybe_fail("after_backup")

        at = now_cn().isoformat(timespec="microseconds")
        prefix_hash = sha256_bytes(b"".join(event_file_lines(task_dir / EVENTS_FILE)))
        reconciliation = {
            "schema": RECONCILIATION_SCHEMA,
            "task_id": task_id,
            "decision_set_id": decision_set_id,
            "decision": row["decision"],
            "target_status": row["target_status"],
            "replacement_task": row.get("replacement_task"),
            "reason": row["reason"],
            "request_id": request_id,
            "actor": "user",
            "actor_model": "human",
            "reconciled_at": at,
            "backup_manifest": backup_reference,
            "backup_manifest_sha256": backup_hash,
        }
        after = _after_status(before, row, at, reconciliation)
        if mode == "valid_tail_bridge":
            ledger_before_hash = last.get("after_hash")
            ledger_from_status = last.get("to_status")
        else:
            ledger_before_hash = status_hash(before)
            ledger_from_status = before.get("status")
        anchor = {
            "schema": RECONCILE_ANCHOR_SCHEMA,
            "task_id": task_id,
            "request_id": request_id,
            "prefix_event_count": len(events),
            "prefix_sha256": prefix_hash,
            "source_integrity": integrity.status,
            "source_integrity_detail": integrity.detail,
            "mode": mode,
            "physical_before_status": before.get("status"),
            "physical_before_status_hash": status_hash(before),
            "decision_set_id": decision_set_id,
            "decision_set_sha256": context["decision_sha256"],
            "decision_ref": context["decision_ref"],
            "decision_task_id": context["decision_task_id"],
            "adjudication_sha256": context["adjudication_sha256"],
            "evidence_manifest_sha256": context["evidence_sha256"],
            "evidence_ref": context["evidence_ref"],
            "expected_files": context["expected_files"],
            "backup_manifest": backup_reference,
            "backup_manifest_sha256": backup_hash,
            "approval_evidence": approval_evidence,
        }
        seq = len(events) + 1
        event = {
            "schema": "board-event/v1",
            "seq": seq,
            "action": RECONCILE_ACTION,
            "actor": "user",
            "actor_model": "human",
            "at": at,
            "request_id": request_id,
            "request_fingerprint": request_fp,
            "from_status": ledger_from_status,
            "to_status": after.get("status"),
            "before_hash": ledger_before_hash,
            "after_hash": status_hash(after),
            "prev_event_hash": last.get("event_hash"),
            "history_scope": RECONCILED_HISTORY_SCOPE,
            "state_revision": seq,
            "after_state": status_projection(after),
            "history_reconciliation": reconciliation,
            "reconcile_anchor": anchor,
        }
        event["event_hash"] = event_hash(event)
        pending = {
            "schema": RECONCILE_PENDING_SCHEMA,
            "task_id": task_id,
            "physical_before_status": before,
            "after_status": after,
            "event": event,
            "expected_files": context["expected_files"],
            "backup_manifest": backup_reference,
            "backup_manifest_sha256": backup_hash,
        }
        pending_path = task_dir / RECONCILE_PENDING_FILE
        atomic_write_json(pending_path, pending)
        _maybe_fail("after_pending")
        atomic_write_json(task_dir / "status.json", after)
        _maybe_fail("after_status")
        append_event(task_dir, event)
        _maybe_fail("after_event")
        pending_path.unlink()
        fsync_directory(task_dir)
        final_integrity = get_task_integrity(task_dir, after)
        if final_integrity.status != "reconciled_history":
            raise TransitionError(
                f"reconcile commit failed postcondition: {final_integrity.status}: "
                f"{final_integrity.detail}"
            )
        return {
            "ok": True,
            "applied": True,
            "idempotent": False,
            "task_id": task_id,
            "source_integrity": integrity.status,
            "mode": mode,
            "decision": row["decision"],
            "status": after.get("status"),
            "event_seq": seq,
            "request_id": request_id,
            "backup_manifest": backup_reference,
            "integrity": final_integrity.status,
        }
