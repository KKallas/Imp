#!/usr/bin/env python3
"""Tests for tools/heuristics/calibrate.py."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.heuristics.calibrate import (
    DEFAULT_DURATIONS,
    calibrate,
    complexity_bucket,
)


def _issue(body: str = "", created: str = "2026-01-01", closed: str = "2026-01-04") -> dict:
    return {
        "body": body,
        "state": "CLOSED",
        "createdAt": f"{created}T00:00:00Z",
        "closedAt": f"{closed}T00:00:00Z",
    }


# ── complexity_bucket ───────────────────────────────────────────────

def test_small_bucket_for_short_issue() -> None:
    issue = _issue(body="Fix the typo.")
    assert complexity_bucket(issue) == "small"


def test_medium_bucket_for_moderate_body() -> None:
    issue = _issue(body="x" * 600)
    assert complexity_bucket(issue) == "medium"


def test_large_bucket_for_long_body() -> None:
    issue = _issue(body="x" * 2000)
    assert complexity_bucket(issue) == "large"


def test_medium_bucket_for_few_checkboxes() -> None:
    body = "\n".join(f"- [ ] Task {i}" for i in range(3))
    issue = _issue(body=body)
    assert complexity_bucket(issue) == "medium"


def test_large_bucket_for_many_checkboxes() -> None:
    body = "\n".join(f"- [ ] Task {i}" for i in range(8))
    issue = _issue(body=body)
    assert complexity_bucket(issue) == "large"


# ── calibrate ───────────────────────────────────────────────────────

def test_empty_input_returns_defaults() -> None:
    result = calibrate([])
    assert result == DEFAULT_DURATIONS


def test_single_bucket_returns_median() -> None:
    issues = [
        _issue(body="short", created="2026-01-01", closed="2026-01-03"),  # 2 days
        _issue(body="short", created="2026-01-01", closed="2026-01-06"),  # 5 days
        _issue(body="short", created="2026-01-01", closed="2026-01-04"),  # 3 days
    ]
    result = calibrate(issues)
    assert result["small"] == 3  # median of [2, 3, 5]
    assert result["medium"] == DEFAULT_DURATIONS["medium"]  # no data
    assert result["large"] == DEFAULT_DURATIONS["large"]  # no data


def test_mixed_buckets() -> None:
    issues = [
        # Small: 2 days
        _issue(body="tiny fix", created="2026-01-01", closed="2026-01-03"),
        # Medium: 5 days
        _issue(body="x" * 700, created="2026-01-01", closed="2026-01-06"),
        # Large: 10 days
        _issue(body="x" * 2000, created="2026-01-01", closed="2026-01-11"),
    ]
    result = calibrate(issues)
    assert result["small"] == 2
    assert result["medium"] == 5
    assert result["large"] == 10


def test_missing_dates_skipped() -> None:
    issues = [
        {"body": "no dates", "state": "CLOSED"},
        _issue(body="has dates", created="2026-01-01", closed="2026-01-04"),
    ]
    result = calibrate(issues)
    assert result["small"] == 3  # only the valid issue counts


def test_negative_delta_skipped() -> None:
    issues = [
        _issue(body="weird", created="2026-01-10", closed="2026-01-05"),  # negative
        _issue(body="normal", created="2026-01-01", closed="2026-01-04"),
    ]
    result = calibrate(issues)
    assert result["small"] == 3


# ── runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    print(f"\nAll {len(tests)} calibration tests passed.")
