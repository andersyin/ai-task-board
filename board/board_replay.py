#!/usr/bin/env python3
"""Read-only historical replay analysis and evidence export.

The replay path never repairs or recovers a task.  It presents current status,
the committed event observation and the strongest reconstruction the ledger can
prove.  Ambiguous or corrupt history always remains a human decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from board_contract import is_physical_task_dir, validate_task_id
from board_integrity import get_task_integrity
from board_transition import (
    EVENTS_FILE,
    PENDING_FILE,
    TransitionError,
    event_hash,
    load_events,
    status_hash,
    status_projection,
    transition_allowed,
)


REPLAY_SCHEMA = "board-replay/v1"
BATCH_SCHEMA = "board-replay-batch/v1"
BUNDLE_SCHEMA = "board-evidence-bundle/v1"
SNAPSHOT_NAMES = ("status.json", "events.jsonl", "card.md", "PROGRESS.md", "REVIEW.md")


class ReplayError(RuntimeError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_status(task_dir: Path) -> tuple[dict, bytes]:
    path = task_dir / "status.json"
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"status.json unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError("status.json must be a JSON object")
    return value, raw


def _issue(code: str, *, seq: int | None = None, expected=None, observed=None, detail: str = "") -> dict:
    value = {"code": code}
    if seq is not None:
        value["seq"] = seq
    if expected is not None:
        value["expected"] = expected
    if observed is not None:
        value["observed"] = observed
    if detail:
        value["detail"] = detail
    return value


def _anchor_valid(first: dict) -> bool:
    scope = first.get("history_scope")
    legacy_archive = first.get("action") == "archive" and not first.get("event_hash")
    if first.get("action") == "create":
        return (
            scope == "full"
            and first.get("from_status") is None
            and first.get("to_status") in {"open", "awaiting_user_approval"}
            and first.get("before_hash") is None
        )
    if first.get("action") == "bootstrap":
        return (
            scope == "partial"
            and first.get("from_status") == first.get("to_status")
            and first.get("before_hash") == first.get("after_hash")
        )
    if legacy_archive:
        return (
            scope == "full"
            and first.get("from_status") in {None, "open"}
            and first.get("to_status") == "done"
            and first.get("prev_event_hash") is None
        )
    return False


def analyze_event_chain(events: list[dict]) -> dict:
    if not events:
        return {
            "count": 0,
            "scope": None,
            "valid": False,
            "issues": [_issue("EVENT_LEDGER_UNAVAILABLE")],
            "events": [],
        }

    issues: list[dict] = []
    checks: list[dict] = []
    scope = events[0].get("history_scope")
    if not _anchor_valid(events[0]):
        issues.append(_issue("ANCHOR_INVALID", seq=1, observed=events[0].get("action")))

    previous = None
    seen_requests: set[str] = set()
    for expected_seq, event in enumerate(events, 1):
        event_issues: list[dict] = []
        schema_seq_ok = event.get("schema") == "board-event/v1" and event.get("seq") == expected_seq
        if not schema_seq_ok:
            event_issues.append(
                _issue(
                    "SCHEMA_OR_SEQUENCE_INVALID",
                    seq=expected_seq,
                    expected={"schema": "board-event/v1", "seq": expected_seq},
                    observed={"schema": event.get("schema"), "seq": event.get("seq")},
                )
            )
        revision = event.get("state_revision")
        if revision is not None and revision != expected_seq:
            event_issues.append(
                _issue("STATE_REVISION_INVALID", seq=expected_seq, expected=expected_seq, observed=revision)
            )

        legacy_archive = event.get("action") == "archive" and not event.get("event_hash")
        if not legacy_archive:
            calculated = event_hash(event)
            if calculated != event.get("event_hash"):
                event_issues.append(
                    _issue(
                        "EVENT_HASH_MISMATCH",
                        seq=expected_seq,
                        expected=calculated,
                        observed=event.get("event_hash"),
                    )
                )

        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in seen_requests:
            event_issues.append(
                _issue("REQUEST_ID_INVALID_OR_DUPLICATE", seq=expected_seq, observed=request_id)
            )
        else:
            seen_requests.add(request_id)

        if event.get("history_scope") != scope:
            event_issues.append(
                _issue(
                    "HISTORY_SCOPE_DRIFT",
                    seq=expected_seq,
                    expected=scope,
                    observed=event.get("history_scope"),
                )
            )

        if previous is None:
            if event.get("prev_event_hash") is not None:
                event_issues.append(
                    _issue(
                        "PREV_EVENT_HASH_BREAK",
                        seq=expected_seq,
                        expected=None,
                        observed=event.get("prev_event_hash"),
                    )
                )
        else:
            if event.get("prev_event_hash") != previous.get("event_hash"):
                event_issues.append(
                    _issue(
                        "PREV_EVENT_HASH_BREAK",
                        seq=expected_seq,
                        expected=previous.get("event_hash"),
                        observed=event.get("prev_event_hash"),
                    )
                )
            if not legacy_archive and event.get("before_hash") != previous.get("after_hash"):
                event_issues.append(
                    _issue(
                        "STATUS_HASH_CHAIN_BREAK",
                        seq=expected_seq,
                        expected=previous.get("after_hash"),
                        observed=event.get("before_hash"),
                    )
                )
            if event.get("from_status") != previous.get("to_status"):
                event_issues.append(
                    _issue(
                        "STATUS_CHAIN_BREAK",
                        seq=expected_seq,
                        expected=previous.get("to_status"),
                        observed=event.get("from_status"),
                    )
                )
            if not transition_allowed(
                event.get("action"), event.get("from_status"), event.get("to_status")
            ):
                event_issues.append(
                    _issue(
                        "ILLEGAL_TRANSITION",
                        seq=expected_seq,
                        observed={
                            "action": event.get("action"),
                            "from_status": event.get("from_status"),
                            "to_status": event.get("to_status"),
                        },
                    )
                )

        issues.extend(event_issues)
        checks.append(
            {
                "seq": expected_seq,
                "action": event.get("action"),
                "from_status": event.get("from_status"),
                "to_status": event.get("to_status"),
                "event_hash": event.get("event_hash"),
                "after_hash": event.get("after_hash"),
                "valid": not event_issues and not (expected_seq == 1 and not _anchor_valid(event)),
                "issues": event_issues,
            }
        )
        previous = event

    return {
        "count": len(events),
        "scope": scope,
        "valid": not issues,
        "issues": issues,
        "events": checks,
    }


def _source_files(task_dir: Path) -> dict:
    result = {}
    for name in SNAPSHOT_NAMES:
        path = task_dir / name
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        result[name] = {"sha256": sha256_bytes(raw), "bytes": len(raw)}
    return result


def _candidate_outcomes(chain_valid: bool) -> list[dict]:
    values = [
        {
            "id": "keep_current_status",
            "impact": "Preserve current status and require a future reconcile event to explain ledger divergence.",
            "requires": ["user task-scoped adjudication", "artifact/Git evidence"],
        },
        {
            "id": "cancel",
            "impact": "Record that historical delivery must not be treated as completed.",
            "requires": ["user task-scoped adjudication", "cancellation reason"],
        },
        {
            "id": "supersede",
            "impact": "Keep history visible and replace the work with a separately identified task.",
            "requires": ["user task-scoped adjudication", "replacement task identity"],
        },
        {
            "id": "keep_quarantine",
            "impact": "Make no historical claim; task remains unschedulable and explicitly corrupt.",
            "requires": ["explicit human decision or insufficient evidence finding"],
        },
    ]
    if chain_valid:
        values.insert(
            1,
            {
                "id": "adopt_event_tail",
                "impact": "Treat the valid committed ledger tail as the candidate current state.",
                "requires": ["user task-scoped adjudication", "downstream impact review"],
            },
        )
    return values


def analyze_task(task_dir: Path) -> dict:
    task_dir = Path(task_dir)
    tasks_root = task_dir.parent
    if (
        not validate_task_id(task_dir.name)
        or not is_physical_task_dir(task_dir, tasks_root)
        or task_dir.is_symlink()
    ):
        raise ReplayError("task path is unsafe or is not a physical direct child of tasks/")

    status, raw_status = _read_status(task_dir)
    integrity = get_task_integrity(task_dir, status)
    try:
        events = load_events(task_dir)
        load_error = None
    except TransitionError as exc:
        events = []
        load_error = str(exc)
    chain = analyze_event_chain(events)
    if load_error:
        chain["issues"].append(_issue("EVENT_LEDGER_UNAVAILABLE", detail=load_error))
        chain["valid"] = False

    if (task_dir / PENDING_FILE).exists():
        chain["issues"].append(_issue("PENDING_JOURNAL_PRESENT"))
        chain["valid"] = False

    projection = status_projection(status)
    current = {
        "raw_sha256": sha256_bytes(raw_status),
        "projection": projection,
        "projection_hash": status_hash(status),
    }
    tail = events[-1] if events else None
    event_tail = None
    if tail:
        event_tail = {
            "seq": tail.get("seq"),
            "action": tail.get("action"),
            "status": tail.get("to_status"),
            "after_hash": tail.get("after_hash"),
            "after_state": tail.get("after_state") if isinstance(tail.get("after_state"), dict) else None,
            "event_hash": tail.get("event_hash"),
        }

    if not chain["valid"]:
        reconstruction = {"completeness": "unavailable", "state": None, "source": None}
    elif event_tail and event_tail["after_state"] is not None:
        reconstruction = {
            "completeness": "full_projection",
            "state": event_tail["after_state"],
            "source": "event_tail.after_state",
        }
    elif event_tail:
        reconstruction = {
            "completeness": "status_only",
            "state": {"status": event_tail["status"]},
            "source": "event_tail.to_status",
        }
    else:
        reconstruction = {"completeness": "unavailable", "state": None, "source": None}

    comparison = {
        "current_vs_tail_status": (
            status.get("status") == event_tail["status"] if event_tail is not None else None
        ),
        "current_hash_vs_tail_after_hash": (
            current["projection_hash"] == event_tail["after_hash"] if event_tail is not None else None
        ),
        "current_projection_vs_tail_after_state": (
            projection == event_tail["after_state"]
            if event_tail is not None and event_tail["after_state"] is not None
            else None
        ),
    }

    reasons = [issue["code"] for issue in chain["issues"]]
    if comparison["current_vs_tail_status"] is False:
        reasons.append("CURRENT_STATUS_DIFFERS_FROM_EVENT_TAIL")
    if comparison["current_hash_vs_tail_after_hash"] is False:
        reasons.append("CURRENT_HASH_DIFFERS_FROM_EVENT_TAIL")
    if comparison["current_projection_vs_tail_after_state"] is False:
        reasons.append("CURRENT_PROJECTION_DIFFERS_FROM_AFTER_STATE")
    reasons = list(dict.fromkeys(reasons))

    if integrity.status == "healthy":
        decision_state = "no_action"
    elif integrity.status == "partial_history":
        decision_state = "compatible_partial"
    elif integrity.status == "legacy_unmanaged":
        decision_state = "compatible_unmanaged"
    else:
        decision_state = "needs_human_decision"
        if not reasons:
            reasons.append(integrity.status.upper())

    return {
        "schema": REPLAY_SCHEMA,
        "task_id": task_dir.name,
        "integrity": integrity.to_dict(),
        "source_files": _source_files(task_dir),
        "current": current,
        "event_chain": chain,
        "event_tail": event_tail,
        "reconstruction": reconstruction,
        "comparison": comparison,
        "decision": {
            "state": decision_state,
            "reasons": reasons,
            "candidate_outcomes": (
                _candidate_outcomes(chain["valid"])
                if decision_state == "needs_human_decision"
                else []
            ),
        },
    }


def _write_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git_timeline(task_dir: Path) -> dict:
    discovered = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=task_dir,
        text=True,
        capture_output=True,
    )
    if discovered.returncode != 0:
        return {"available": False, "repository": None, "commits": []}
    repository = Path(discovered.stdout.strip()).resolve()
    try:
        relative = task_dir.resolve().relative_to(repository).as_posix()
    except ValueError:
        return {"available": False, "repository": str(repository), "commits": []}
    logged = subprocess.run(
        ["git", "log", "--format=%H%x09%aI%x09%an%x09%s", "--", relative],
        cwd=repository,
        text=True,
        capture_output=True,
    )
    commits = []
    if logged.returncode == 0:
        for line in logged.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                commits.append(
                    {"commit": parts[0], "at": parts[1], "author": parts[2], "subject": parts[3]}
                )
    return {
        "available": logged.returncode == 0,
        "repository": str(repository),
        "task_path": relative,
        "commits": commits,
    }


def _artifact_inventory(task_dir: Path) -> dict:
    status, _ = _read_status(task_dir)
    local_output = []
    output_dir = task_dir / "output"
    if output_dir.is_dir() and not output_dir.is_symlink():
        for path in sorted(output_dir.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            local_output.append(
                {
                    "path": path.relative_to(task_dir).as_posix(),
                    "bytes": len(raw),
                    "sha256": sha256_bytes(raw),
                }
            )
    references = []
    for field in ("submission_outputs", "submission_evidence"):
        values = status.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            references.append({"field": field, "value": value})
    return {
        "progress_present": (task_dir / "PROGRESS.md").is_file(),
        "review_present": (task_dir / "REVIEW.md").is_file(),
        "local_output_files": local_output,
        "submission_references": references,
    }


def enrich_bundle(task_dir: Path, analysis: dict, bundle_root: Path) -> Path:
    """Add read-only Git/artifact observations to an already verified bundle."""
    task_dir = Path(task_dir).resolve()
    target = Path(bundle_root).resolve() / task_dir.name
    manifest_path = target / "manifest.json"
    analysis_path = target / "analysis.json"
    if not target.is_dir() or target.is_symlink() or not manifest_path.is_file():
        raise ReplayError(f"bundle does not exist or is unsafe: {target}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != BUNDLE_SCHEMA or manifest.get("task_id") != task_dir.name:
        raise ReplayError(f"bundle manifest identity mismatch: {target}")
    analysis_raw = analysis_path.read_bytes()
    if sha256_bytes(analysis_raw) != manifest.get("analysis_sha256"):
        raise ReplayError(f"bundle analysis hash mismatch: {target}")
    stored_analysis = json.loads(analysis_raw.decode("utf-8"))
    if stored_analysis != analysis:
        raise ReplayError(f"bundle/current analysis drift before enrichment: {task_dir.name}")
    for name, fingerprint in manifest.get("files", {}).items():
        source_raw = (task_dir / name).read_bytes()
        snapshot_raw = (target / "snapshots" / name).read_bytes()
        if (
            source_raw != snapshot_raw
            or sha256_bytes(source_raw) != fingerprint.get("sha256")
            or len(source_raw) != fingerprint.get("bytes")
        ):
            raise ReplayError(f"bundle/source drift before enrichment: {task_dir.name}/{name}")
    extras = {
        "git_timeline.json": _git_timeline(task_dir),
        "artifact_inventory.json": _artifact_inventory(task_dir),
    }
    if any((target / name).exists() for name in extras):
        raise ReplayError(f"bundle is already enriched: {target}")
    supplemental = {}
    for name, value in extras.items():
        path = target / name
        _write_json(path, value)
        raw = path.read_bytes()
        supplemental[name] = {"sha256": sha256_bytes(raw), "bytes": len(raw)}
    manifest["supplemental_files"] = supplemental
    _write_json(manifest_path, manifest)
    return target


def _bundle_readme(analysis: dict) -> str:
    lines = [
        f"# Evidence Bundle — {analysis['task_id']}",
        "",
        "> Read-only snapshot. This bundle is evidence, not a reconciliation decision.",
        "",
        f"- integrity: `{analysis['integrity']['status']}`",
        f"- detail: {analysis['integrity'].get('detail') or 'n/a'}",
        f"- decision: `{analysis['decision']['state']}`",
        f"- current status: `{analysis['current']['projection'].get('status')}`",
        f"- event-tail status: `{(analysis.get('event_tail') or {}).get('status')}`",
        "",
        "## Reasons",
        "",
    ]
    reasons = analysis["decision"].get("reasons") or []
    lines.extend([f"- `{reason}`" for reason in reasons] or ["- none"])
    lines.extend(["", "## Candidate outcomes (unordered)", ""])
    candidates = analysis["decision"].get("candidate_outcomes") or []
    lines.extend([f"- `{item['id']}` — {item['impact']}" for item in candidates] or ["- none"])
    lines.extend(["", "## Snapshots", ""])
    lines.extend([f"- `{name}`" for name in analysis["source_files"]])
    lines.extend(
        [
            "",
            "No candidate is selected by this file. Phase 2B requires a user decision bound to this task ID.",
            "",
        ]
    )
    return "\n".join(lines)


def export_bundle(task_dir: Path, analysis: dict, bundle_root: Path) -> Path:
    task_dir = Path(task_dir).resolve()
    bundle_root = Path(bundle_root)
    resolved_root = bundle_root.resolve(strict=False)
    try:
        resolved_root.relative_to(task_dir)
    except ValueError:
        pass
    else:
        raise ReplayError("bundle target must not be inside the source task directory")

    bundle_root.mkdir(parents=True, exist_ok=True)
    if bundle_root.is_symlink():
        raise ReplayError("bundle root must not be a symlink")
    target = bundle_root / task_dir.name
    if target.exists() or target.is_symlink():
        raise ReplayError(f"bundle already exists: {target}")

    temporary = Path(tempfile.mkdtemp(prefix=f".{task_dir.name}.", dir=str(bundle_root)))
    try:
        snapshots = temporary / "snapshots"
        snapshots.mkdir()
        manifest_files = {}
        for name, fingerprint in analysis["source_files"].items():
            source = task_dir / name
            destination = snapshots / name
            raw = source.read_bytes()
            if sha256_bytes(raw) != fingerprint["sha256"] or len(raw) != fingerprint["bytes"]:
                raise ReplayError(f"source changed during export: {task_dir.name}/{name}")
            destination.write_bytes(raw)
            manifest_files[name] = dict(fingerprint)

        _write_json(temporary / "analysis.json", analysis)
        (temporary / "README.md").write_text(_bundle_readme(analysis), encoding="utf-8")
        analysis_raw = (temporary / "analysis.json").read_bytes()
        manifest = {
            "schema": BUNDLE_SCHEMA,
            "task_id": task_dir.name,
            "source_task": f"tasks/{task_dir.name}",
            "files": manifest_files,
            "analysis_sha256": sha256_bytes(analysis_raw),
            "decision_state": analysis["decision"]["state"],
        }
        _write_json(temporary / "manifest.json", manifest)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target
