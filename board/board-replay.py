#!/usr/bin/env python3
"""CLI for read-only historical task replay and explicit evidence export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from board_replay import BATCH_SCHEMA, ReplayError, analyze_task, enrich_bundle, export_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="历史任务只读 replay / evidence export")
    parser.add_argument("--board-root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--task", action="append", required=True, help="任务 ID；可重复")
    parser.add_argument("--bundle-dir", help="显式导出证据包；省略时严格零写入")
    parser.add_argument(
        "--enrich-existing",
        action="store_true",
        help="验证已有 bundle 后补 Git 时间线与产物清单；不覆盖 snapshots/analysis",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    board_root = Path(args.board_root).resolve()
    tasks_root = board_root / "tasks"
    results = []
    bundles = []
    try:
        for task_id in args.task:
            task_dir = tasks_root / task_id
            analysis = analyze_task(task_dir)
            results.append(analysis)
            if args.bundle_dir:
                if args.enrich_existing:
                    bundle = enrich_bundle(task_dir, analysis, Path(args.bundle_dir).resolve())
                else:
                    bundle = export_bundle(task_dir, analysis, Path(args.bundle_dir).resolve())
                bundles.append(str(bundle))
    except (ReplayError, OSError) as exc:
        print(f"REPLAY_ERROR: {exc}", file=sys.stderr)
        return 2

    payload = {
        "schema": BATCH_SCHEMA,
        "count": len(results),
        "results": results,
        "bundles": bundles,
    }
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
