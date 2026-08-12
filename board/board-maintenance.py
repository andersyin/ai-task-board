#!/usr/bin/env python3
"""Explicit persistent maintenance entrypoint. No writes without --apply."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


APPLY_COMMANDS = (
    ("board-lease-check.py", "--mode", "apply"),
    # P0-2 验收超时自动升级处置（2026-08-12 治理新增）：扫 awaiting_review 超 4h，
    # 有路自动 wake review 唤醒验收端，堵死输出需人工授权。max-wakes=2 防惊群。
    ("board-review-escalate.py", "--threshold-hours", "4", "--max-wakes", "2"),
    ("board-index-gen.py", "--mode", "apply"),
    ("board-alert.py", "--mode", "apply"),
    ("board-dashboard.py", "--mode", "apply"),
    ("board-validate.py",),
    # v1.5: 间接验证标记（下游 PASS 时标记上游 pass_to_next 任务）
    ("board-indirect-verify.py", "--apply"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="persistent board maintenance")
    parser.add_argument("--apply", action="store_true", help="required acknowledgement for writes")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    args = parser.parse_args()
    if not args.apply:
        parser.error("maintenance writes require explicit --apply")
    board_root = Path(args.board_root).resolve()
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    failures = 0
    for spec in APPLY_COMMANDS:
        script, *extra = spec
        result = subprocess.run(
            [sys.executable, str(board_root / script), *extra, "--board-root", str(board_root)],
            cwd=board_root,
            text=True,
            capture_output=True,
            env=env,
        )
        print(f"[{script}] rc={result.returncode}")
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        failures += result.returncode != 0
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

