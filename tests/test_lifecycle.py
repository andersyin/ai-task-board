#!/usr/bin/env python3
"""First-run CLI lifecycle against an isolated --board-root (no live KB)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOARD_SCRIPTS = REPO / "board"


def _run(script: str, *args: str, board_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BOARD_SCRIPTS / script),
            "--board-root",
            str(board_root),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert result.returncode == 0, (
        f"exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    for needle in needles:
        assert needle in combined, f"missing {needle!r} in:\n{combined}"


def _status(board_root: Path, task_id: str) -> dict:
    return json.loads(
        (board_root / "tasks" / task_id / "status.json").read_text(encoding="utf-8")
    )


def _create_hello(board_root: Path, task_id: str = "T01-hello") -> subprocess.CompletedProcess[str]:
    return _run(
        "board-task-create.py",
        "--id",
        task_id,
        "--title",
        "示例任务",
        "--work-key",
        f"demo-{task_id}",
        "--required-caps",
        "scheduled-task",
        "--created-by",
        "trae",
        "--complexity",
        "L1",
        "--delivery",
        "return_result",
        "--acceptance",
        "产物存在且可读",
        "--output",
        "output/result.txt",
        "--evidence",
        "cat output/result.txt",
        "--body",
        "写一个可读的示例产物。",
        board_root=board_root,
    )


def test_create_rejects_unregistered_agent(tmp_path: Path) -> None:
    result = _run(
        "board-task-create.py",
        "--id",
        "T01-hello",
        "--title",
        "示例任务",
        "--work-key",
        "demo-hello",
        "--required-caps",
        "scheduled-task",
        "--created-by",
        "your-agent",
        "--complexity",
        "L1",
        "--acceptance",
        "产物存在且可读",
        "--output",
        "output/result.txt",
        "--evidence",
        "cat output/result.txt",
        "--body",
        "写一个可读的示例产物。",
        board_root=tmp_path,
    )
    assert result.returncode != 0
    assert "已注册端" in result.stderr


def test_create_requires_v3_contract_fields(tmp_path: Path) -> None:
    result = _run(
        "board-task-create.py",
        "--id",
        "T01-hello",
        "--title",
        "示例任务",
        "--work-key",
        "demo-hello",
        "--required-caps",
        "scheduled-task",
        "--created-by",
        "trae",
        "--complexity",
        "L1",
        "--body",
        "缺少 acceptance/output/evidence 应失败。",
        board_root=tmp_path,
    )
    assert result.returncode != 0
    assert "qualified contract invalid" in result.stderr


def test_happy_path_create_claim_submit_review(tmp_path: Path) -> None:
    task_id = "T01-hello"
    task_dir = tmp_path / "tasks" / task_id

    created = _create_hello(tmp_path, task_id)
    _assert_ok(created, "任务已创建")
    status = _status(tmp_path, task_id)
    assert status["status"] == "open"
    fingerprint = status["contract_fingerprint"]
    assert fingerprint
    assert (task_dir / "card.md").is_file()
    assert (task_dir / "contract.json").is_file()
    assert (task_dir / "events.jsonl").is_file()

    claimed = _run(
        "board-task-claim.py",
        "--task",
        task_id,
        "--agent",
        "workbuddy",
        "--model",
        "deepseek-v4-flash",
        board_root=tmp_path,
    )
    _assert_ok(claimed, "已原子认领")
    assert _status(tmp_path, task_id)["status"] == "claimed"

    raced = _run(
        "board-task-claim.py",
        "--task",
        task_id,
        "--agent",
        "codex",
        "--model",
        "gpt-5.6-sol",
        board_root=tmp_path,
    )
    assert raced.returncode == 3, raced.stderr

    (task_dir / "output" / "result.txt").write_text("hello from the board\n", encoding="utf-8")
    (task_dir / "PROGRESS.md").write_text(
        "[workbuddy/deepseek-v4-flash] 已交付 output/result.txt\n",
        encoding="utf-8",
    )
    submitted = _run(
        "board-task-transition.py",
        "--task",
        task_id,
        "--action",
        "submit",
        "--actor",
        "workbuddy",
        "--model",
        "deepseek-v4-flash",
        "--request-id",
        "req-001",
        "--output-ref",
        "output/result.txt",
        "--evidence-ref",
        "cat:output/result.txt",
        board_root=tmp_path,
    )
    _assert_ok(submitted, "awaiting_review")
    assert _status(tmp_path, task_id)["status"] == "awaiting_review"

    (task_dir / "REVIEW.md").write_text(
        f"验收端: workbuddy/kimi-k3-1\n验收结果: PASS\n合同指纹: {fingerprint}\n问题数: 0\n",
        encoding="utf-8",
    )
    self_review = _run(
        "board-task-transition.py",
        "--task",
        task_id,
        "--action",
        "review",
        "--actor",
        "workbuddy",
        "--model",
        "kimi-k3-1",
        "--request-id",
        "req-self",
        "--result",
        "pass",
        "--issues",
        "0",
        board_root=tmp_path,
    )
    assert self_review.returncode != 0
    assert _status(tmp_path, task_id)["status"] == "awaiting_review"

    (task_dir / "REVIEW.md").write_text(
        f"验收端: trae/glm-5.2\n验收结果: PASS\n合同指纹: {fingerprint}\n问题数: 0\n",
        encoding="utf-8",
    )
    reviewed = _run(
        "board-task-transition.py",
        "--task",
        task_id,
        "--action",
        "review",
        "--actor",
        "trae",
        "--model",
        "glm-5.2",
        "--request-id",
        "req-002",
        "--result",
        "pass",
        "--issues",
        "0",
        board_root=tmp_path,
    )
    _assert_ok(reviewed, "done")
    assert _status(tmp_path, task_id)["status"] == "done"

    replayed = _run(
        "board-task-transition.py",
        "--task",
        task_id,
        "--action",
        "review",
        "--actor",
        "trae",
        "--model",
        "glm-5.2",
        "--request-id",
        "req-002",
        "--result",
        "pass",
        "--issues",
        "0",
        board_root=tmp_path,
    )
    _assert_ok(replayed, "幂等命中")


def test_setup_list_uses_contract_review_levels() -> None:
    result = subprocess.run(
        [sys.executable, str(BOARD_SCRIPTS / "board-setup.py"), "--list"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # 2026-08-12 治理：workbuddy 验收上限 L3，列表角色不能再写成 L1/L2验收。
    workbuddy_lines = [line for line in result.stdout.splitlines() if "workbuddy" in line.lower()]
    assert workbuddy_lines, result.stdout
    assert any("L1/L2/L3验收" in line for line in workbuddy_lines), workbuddy_lines


def test_lease_check_runner_requires_kb_root(tmp_path: Path) -> None:
    script = BOARD_SCRIPTS / "board-lease-check-runner.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 1
    assert "KB_ROOT" in result.stderr
