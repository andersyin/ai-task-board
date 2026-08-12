#!/usr/bin/env python3
"""Canonical review verdict contract shared by runtime consumers and generators."""

from __future__ import annotations

import re


REVIEW_RESULTS = ("pass", "fail")
REVIEW_LABELS = tuple(result.upper() for result in REVIEW_RESULTS)
REVIEW_DISPLAY = "|".join(REVIEW_LABELS)
REVIEW_CLI_DISPLAY = "|".join(REVIEW_RESULTS)

# Read-only compatibility only. Never expose these as accepted current verdicts.
LEGACY_REVIEW_RESULTS = ("conditional_pass",)
LEGACY_REVIEW_LABELS = ("CONDITIONAL_PASS",)


def parse_current_verdict(text: str) -> str | None:
    match = re.search(
        r"^\s*(?:#+\s*)?验收结果\s*[:：]\s*(PASS|FAIL)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).lower() if match else None


def parse_legacy_verdict(text: str) -> str | None:
    current = parse_current_verdict(text)
    if current:
        return current
    match = re.search(
        r"^\s*(?:#+\s*)?验收结果\s*[:：]\s*CONDITIONAL_PASS(?:\s|$|（)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    return "conditional_pass" if match else None
