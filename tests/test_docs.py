#!/usr/bin/env python3
"""Guard first-run docs against commands the CLIs will reject."""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_readme_quickstart_matches_real_clis() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "board-task-claim.py" in text
    assert "--action claim" not in text
    assert "--verdict PASS" not in text
    assert "--required-caps" in text
    assert "--created-by" in text
    assert "--delivery return_result" in text
    assert "--actor" in text
    assert "--result pass" in text
    assert "templates/REVIEW.md" in text


def test_examples_lifecycle_matches_real_clis() -> None:
    text = (REPO / "examples" / "README.md").read_text(encoding="utf-8")
    assert "board-task-claim.py" in text
    assert "--action claim" not in text
    assert "--verdict PASS" not in text
    assert "--delivery return_result" in text
    assert "--actor user" in text
    assert "--approval-evidence" in text


def test_skill_protocol_link_exists() -> None:
    skill = (REPO / "docs" / "SKILL.md").read_text(encoding="utf-8")
    assert "](protocol.md)" in skill
    assert (REPO / "docs" / "protocol.md").is_file()
    assert (REPO / "board" / "templates" / "REVIEW.md").is_file()
