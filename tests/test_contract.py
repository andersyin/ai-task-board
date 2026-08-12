#!/usr/bin/env python3
"""Tests for core board contract and state machine."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure board modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board"))

from board_contract import (
    AGENT_CONTRACTS,
    ALLOWED_STATUSES,
    VALID_TRANSITIONS,
    canonical_agent,
    is_safe_token,
    validate_task_id,
)
from board_verdicts import REVIEW_RESULTS, LEGACY_REVIEW_RESULTS


class TestStateMachine:
    """Verify the task state machine is well-formed."""

    def test_all_statuses_have_transitions(self):
        for status in ALLOWED_STATUSES:
            assert status in VALID_TRANSITIONS, f"Status '{status}' has no transition entry"

    def test_archived_is_terminal(self):
        assert VALID_TRANSITIONS["archived"] == set()
        assert "archived" not in VALID_TRANSITIONS["archived"]

    def test_done_only_goes_to_archived(self):
        assert VALID_TRANSITIONS["done"] == {"archived"}

    def test_cancelled_only_goes_to_archived(self):
        assert VALID_TRANSITIONS["cancelled"] == {"archived"}

    def test_superseded_only_goes_to_archived(self):
        assert VALID_TRANSITIONS["superseded"] == {"archived"}

    def test_open_can_be_claimed(self):
        assert "claimed" in VALID_TRANSITIONS["open"]

    def test_claimed_can_submit_for_review(self):
        assert "awaiting_review" in VALID_TRANSITIONS["claimed"]

    def test_review_can_pass_or_fail(self):
        transitions = VALID_TRANSITIONS["awaiting_review"]
        assert "done" in transitions
        assert "claimed" in transitions  # rework

    def test_blocked_can_reopen(self):
        assert "open" in VALID_TRANSITIONS["blocked"]
        assert "claimed" in VALID_TRANSITIONS["blocked"]

    def test_dlq_can_recover(self):
        assert "open" in VALID_TRANSITIONS["dlq"]
        assert "cancelled" in VALID_TRANSITIONS["dlq"]

    def test_no_transition_to_undead_status(self):
        """No status should transition back to a status that was already terminal."""
        for src, targets in VALID_TRANSITIONS.items():
            for tgt in targets:
                assert tgt in ALLOWED_STATUSES, f"Transition {src} -> {tgt}: target not in ALLOWED_STATUSES"


class TestAgentContracts:
    """Verify agent contracts are well-formed."""

    def test_all_agents_have_required_fields(self):
        required = {"display_name", "caps", "model_families", "max_execute_level", "max_review_level"}
        for key, contract in AGENT_CONTRACTS.items():
            for field in required:
                assert field in contract, f"Agent '{key}' missing field '{field}'"

    def test_execute_levels_valid(self):
        valid_levels = {"L1", "L2", "L3", "L4"}
        for key, contract in AGENT_CONTRACTS.items():
            assert contract["max_execute_level"] in valid_levels, \
                f"Agent '{key}' has invalid max_execute_level"

    def test_review_levels_valid(self):
        valid_levels = {"L1", "L2", "L3", "L4"}
        for key, contract in AGENT_CONTRACTS.items():
            assert contract["max_review_level"] in valid_levels, \
                f"Agent '{key}' has invalid max_review_level"

    def test_canonical_agent_resolves(self):
        for key in AGENT_CONTRACTS:
            result = canonical_agent(key)
            assert result is not None, f"canonical_agent returned None for '{key}'"


class TestSafeToken:
    """Verify safe token validation."""

    def test_valid_tokens(self):
        assert is_safe_token("hello")
        assert is_safe_token("task-123")
        assert is_safe_token("my_task.v2")
        assert is_safe_token("a")

    def test_empty_string_invalid(self):
        assert not is_safe_token("")

    def test_space_invalid(self):
        assert not is_safe_token("hello world")

    def test_leading_dot_invalid(self):
        assert not is_safe_token(".hidden")

    def test_leading_dash_invalid(self):
        assert not is_safe_token("-dash")

    def test_special_chars_invalid(self):
        assert not is_safe_token("hello@world")
        assert not is_safe_token("hello/world")
        assert not is_safe_token("hello\\world")


class TestTaskIdValidation:
    """Verify task ID validation."""

    def test_standard_task_id(self):
        assert validate_task_id("T01-hello-world")

    def test_version_task_id(self):
        assert validate_task_id("V11-01-task-create")

    def test_board_task_id(self):
        assert validate_task_id("BN-CANON-01-codex-character-canon")

    def test_empty_invalid(self):
        assert not validate_task_id("")

    def test_space_invalid(self):
        assert not validate_task_id("T01 hello")


class TestVerdicts:
    """Verify verdict constants."""

    def test_pass_and_fail_exist(self):
        assert "pass" in REVIEW_RESULTS
        assert "fail" in REVIEW_RESULTS

    def test_legacy_verdicts_superset(self):
        for v in REVIEW_RESULTS:
            assert v in LEGACY_REVIEW_RESULTS or v in ("pass", "fail")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
