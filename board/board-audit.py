#!/usr/bin/env python3
"""Strict read-only board audit with an exact full-tree content hash guard."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


READONLY_COMMANDS = (
    ("board-index-gen.py", "--mode", "readonly"),
    ("board-lease-check.py", "--mode", "readonly"),
    ("board-alert.py", "--mode", "readonly"),
    ("board-dashboard.py", "--mode", "readonly"),
    ("board-validate.py",),
)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f"L\0{relative}\0{os.readlink(path)}\n".encode("utf-8"))
        elif path.is_file():
            digest.update(f"F\0{relative}\0".encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\n")
        elif path.is_dir():
            digest.update(f"D\0{relative}\n".encode("utf-8"))
    return digest.hexdigest()


def run_once(board_root: Path) -> dict:
    before = tree_digest(board_root)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    commands = []
    for spec in READONLY_COMMANDS:
        script, *extra = spec
        result = subprocess.run(
            [sys.executable, str(board_root / script), *extra, "--board-root", str(board_root)],
            cwd=board_root,
            text=True,
            capture_output=True,
            env=env,
        )
        commands.append(
            {
                "script": script,
                "returncode": result.returncode,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            }
        )
    after = tree_digest(board_root)
    return {
        "before_hash": before,
        "after_hash": after,
        "unchanged": before == after,
        "commands": commands,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="strict read-only board audit")
    parser.add_argument("--board-root", default=str(Path(__file__).parent))
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be between 1 and 100")
    board_root = Path(args.board_root).resolve()
    runs = [run_once(board_root) for _ in range(args.repeat)]
    unchanged = all(run["unchanged"] for run in runs)
    command_failures = sum(
        command["returncode"] != 0
        for run in runs
        for command in run["commands"]
    )
    report = {
        "mode": "readonly",
        "board_root": str(board_root),
        "runs": len(runs),
        "unchanged": unchanged,
        "command_failures": command_failures,
        "tree_hash": runs[-1]["after_hash"] if runs else None,
        "details": runs,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not unchanged:
        return 2
    return 1 if command_failures else 0


if __name__ == "__main__":
    sys.exit(main())

