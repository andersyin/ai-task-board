#!/usr/bin/env python3
"""Formal CLI for one audited historical reconciliation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from board_reconcile import reconcile_history, recover_reconcile_task
from board_transition import TransitionError


DEFAULT_BOARD_ROOT = Path(__file__).resolve().parent


def _refresh_index(board_root: Path) -> None:
    script = board_root / "board-index-gen.py"
    if script.is_file():
        subprocess.run(
            [sys.executable, str(script), "--board-root", str(board_root), "--mode", "apply"],
            check=False,
            capture_output=True,
            text=True,
        )


def execute(args: argparse.Namespace) -> int:
    board_root = Path(args.board_root).resolve()
    if args.recover:
        event = recover_reconcile_task(board_root, args.task)
        _refresh_index(board_root)
        print(json.dumps({
            "ok": True,
            "recovered": bool(event),
            "task_id": args.task,
            "event_seq": event.get("seq") if event else None,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    result = reconcile_history(
        board_root,
        args.task,
        decision_file=Path(args.decision_file),
        decision_set_id=args.decision_set_id,
        evidence_manifest=Path(args.evidence_manifest),
        actor=args.actor,
        actor_model=args.model,
        request_id=args.request_id,
        approval_evidence=args.approval_evidence,
        apply=args.apply,
    )
    if args.apply:
        _refresh_index(board_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="单任务、证据绑定、可恢复的历史 reconcile 正式治理入口"
    )
    parser.add_argument("--task", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--recover", action="store_true")
    parser.add_argument("--decision-file")
    parser.add_argument("--decision-set-id")
    parser.add_argument("--evidence-manifest")
    parser.add_argument("--actor")
    parser.add_argument("--model")
    parser.add_argument("--request-id")
    parser.add_argument("--approval-evidence", default="")
    parser.add_argument("--board-root", default=str(DEFAULT_BOARD_ROOT))
    args = parser.parse_args()
    if not args.recover:
        required = (
            "decision_file",
            "decision_set_id",
            "evidence_manifest",
            "actor",
            "model",
            "request_id",
        )
        missing = [name for name in required if not getattr(args, name)]
        if missing:
            parser.error("dry-run/apply 缺少参数: " + ", ".join(missing))
    try:
        return execute(args)
    except (TransitionError, OSError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
