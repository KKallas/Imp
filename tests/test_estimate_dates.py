"""Tests for pipeline/estimate_dates.py.

Run directly: `.venv/bin/python tests/test_estimate_dates.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Strategy: build a known enriched payload inline (no dependency on
`.imp/enriched.json` existing), run estimate_in_place, and verify
the synthesized fields + touched-issue list are correct. The gh
push path is tested by monkey-patching `run_gh` with a scripted fake.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import estimate_dates as ed  # noqa: E402

TODAY = date(2026, 4, 15)
_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-estdates-test-"))


def _enriched_with_no_dates() -> dict:
    """A minimal enriched payload where no issue has start_date /
    end_date — the realistic case for a repo with no GH Project."""
    return {
        "synced_at": "2026-04-15T06:00:00+00:00",
        "enriched_at": "2026-04-15T06:00:10+00:00",
        "repo": "test/repo",
        "project_number": None,
        "project_owner": None,
        "issue_count": 2,
        "issues": [
            {
                "number": 1,
                "title": "first task",
                "body": "Original body, no dates.",
                "state": "OPEN",
                "createdAt": "2026-04-11T09:00:00Z",
                "updatedAt": "2026-04-11T09:00:00Z",
                "labels": [],
                "milestone": None,
                "assignees": [],
                "fields": {
                    "duration_days": {
                        "value": 4,
                        "source": "heuristic",
                        "confidence": "medium",
                    },
                },
                "depends_on_parsed": [],
            },
            {
                "number": 2,
                "title": "depends on #1",
                "body": "",
                "state": "OPEN",
                "createdAt": "2026-04-12T09:00:00Z",
                "updatedAt": "2026-04-12T09:00:00Z",
                "labels": [],
                "milestone": None,
                "assignees": [],
                "fields": {
                    "duration_days": {
                        "value": 2,
                        "source": "heuristic",
                        "confidence": "medium",
                    },
                },
                "depends_on_parsed": [1],
            },
        ],
        "dependency_edges": [{"from": 2, "to": 1}],
        "delayed_count": 0,
    }


# ---------- body-block format ----------


def test_render_body_block_includes_only_set_fields() -> None:
    block = ed.render_body_block({"start_date": "2026-04-11", "end_date": "2026-04-14"})
    assert "start_date: 2026-04-11" in block
    assert "end_date: 2026-04-14" in block
    # duration_days wasn't in the input — don't emit an empty row.
    assert "duration_days" not in block
    assert block.startswith("<!-- imp:dates:begin -->")
    assert block.endswith("<!-- imp:dates:end -->")
    print("test_render_body_block_includes_only_set_fields: OK")


def test_render_body_block_skips_empty_values() -> None:
    block = ed.render_body_block(
        {"start_date": "2026-04-11", "end_date": None, "duration_days": ""}
    )
    assert "start_date" in block
    assert "end_date" not in block
    assert "duration_days" not in block
    print("test_render_body_block_skips_empty_values: OK")


def test_upsert_body_block_appends_when_absent() -> None:
    body = "Some original body text.\nSecond line."
    new = ed.upsert_body_block(
        body,
        ed.render_body_block({"start_date": "2026-04-11"}),
    )
    # Original content preserved, block appended with a gap.
    assert "Some original body text." in new
    assert "Second line." in new
    assert "<!-- imp:dates:begin -->" in new
    assert new.count("<!-- imp:dates:begin -->") == 1
    print("test_upsert_body_block_appends_when_absent: OK")


def test_upsert_body_block_replaces_when_present() -> None:
    body = (
        "Before\n\n"
        "<!-- imp:dates:begin -->\n"
        "start_date: 2026-04-01\n"
        "<!-- imp:dates:end -->\n\n"
        "After"
    )
    new_block = ed.render_body_block(
        {"start_date": "2026-04-11", "end_date": "2026-04-14"}
    )
    new = ed.upsert_body_block(body, new_block)
    assert "Before" in new
    assert "After" in new
    # Old date replaced, new dates present.
    assert "2026-04-01" not in new
    assert "2026-04-11" in new
    assert "2026-04-14" in new
    # Still exactly one block.
    assert new.count("<!-- imp:dates:begin -->") == 1
    print("test_upsert_body_block_replaces_when_present: OK")


def test_upsert_body_block_handles_empty_body() -> None:
    block = ed.render_body_block({"start_date": "2026-04-11"})
    new = ed.upsert_body_block("", block)
    assert "<!-- imp:dates:begin -->" in new
    assert "start_date: 2026-04-11" in new
    print("test_upsert_body_block_handles_empty_body: OK")


# ---------- estimate_in_place ----------


def test_estimate_in_place_fills_missing_dates() -> None:
    enriched = _enriched_with_no_dates()
    updated, touched = ed.estimate_in_place(enriched, today=TODAY)

    # Every issue should now have start_date + end_date envelopes.
    for issue in updated["issues"]:
        assert "start_date" in issue["fields"], issue["number"]
        assert "end_date" in issue["fields"], issue["number"]
        assert issue["fields"]["start_date"]["source"] == "synthesized"
        assert issue["fields"]["end_date"]["source"] == "synthesized"

    # Both issues had no dates before; both should be touched.
    assert set(touched) == {1, 2}
    print("test_estimate_in_place_fills_missing_dates: OK")


def test_estimate_in_place_skips_already_populated() -> None:
    """If an issue already has project-board dates, synthesize_dates
    leaves it alone — the issue shouldn't show up in `touched`."""
    enriched = _enriched_with_no_dates()
    enriched["issues"][0]["fields"]["start_date"] = {
        "value": "2026-03-01",
        "source": "github",
        "confidence": "high",
    }
    enriched["issues"][0]["fields"]["end_date"] = {
        "value": "2026-03-15",
        "source": "github",
        "confidence": "high",
    }

    _updated, touched = ed.estimate_in_place(enriched, today=TODAY)
    # #1 had real dates → not touched. #2 had nothing → touched.
    assert touched == [2]
    print("test_estimate_in_place_skips_already_populated: OK")


def test_estimate_in_place_is_idempotent() -> None:
    """Running twice produces the same touched list on the first run
    and no new touches on the second — the second pass sees everything
    already synthesized."""
    enriched = _enriched_with_no_dates()
    pass1, touched1 = ed.estimate_in_place(enriched, today=TODAY)
    _pass2, touched2 = ed.estimate_in_place(pass1, today=TODAY)
    assert set(touched1) == {1, 2}
    assert touched2 == []
    print("test_estimate_in_place_is_idempotent: OK")


def test_estimate_in_place_sets_estimated_at_timestamp() -> None:
    enriched = _enriched_with_no_dates()
    updated, _touched = ed.estimate_in_place(enriched, today=TODAY)
    assert "estimated_at" in updated
    assert "T" in updated["estimated_at"]  # ISO 8601
    print("test_estimate_in_place_sets_estimated_at_timestamp: OK")


# ---------- push to github ----------


class _FakeGh:
    """Minimal scripted fake for estimate_dates.run_gh."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.script: list[tuple[int, str, str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if self.script:
            return self.script.pop(0)
        return (0, "", "")


def test_push_to_github_edits_only_touched_issues() -> None:
    fake = _FakeGh()
    ed.run_gh = fake
    enriched = _enriched_with_no_dates()
    updated, touched = ed.estimate_in_place(enriched, today=TODAY)
    report = ed.push_to_github(updated, touched)

    # One `gh issue edit` per touched issue.
    assert report["pushed"] == 2
    assert report["failed"] == 0
    assert len(fake.calls) == 2
    numbers_edited = {call[3] for call in fake.calls}  # `gh issue edit <n>`
    assert numbers_edited == {"1", "2"}
    # The --body argument must contain the dates block with real values.
    for call in fake.calls:
        body_idx = call.index("--body") + 1
        assert "<!-- imp:dates:begin -->" in call[body_idx]
        assert "start_date:" in call[body_idx]
    print("test_push_to_github_edits_only_touched_issues: OK")


def test_push_to_github_preserves_original_body() -> None:
    fake = _FakeGh()
    ed.run_gh = fake
    enriched = _enriched_with_no_dates()
    original = enriched["issues"][0]["body"]
    assert original, "fixture should have non-empty body to test preservation"
    updated, touched = ed.estimate_in_place(enriched, today=TODAY)
    ed.push_to_github(updated, touched)

    call_for_1 = next(c for c in fake.calls if c[3] == "1")
    body_idx = call_for_1.index("--body") + 1
    new_body = call_for_1[body_idx]
    assert original in new_body, "original body content must be preserved"
    print("test_push_to_github_preserves_original_body: OK")


def test_push_to_github_reports_gh_failure() -> None:
    fake = _FakeGh()
    fake.script = [(1, "", "gh: command not found")]
    ed.run_gh = fake
    enriched = _enriched_with_no_dates()
    updated, touched = ed.estimate_in_place(enriched, today=TODAY)
    # Only test against the first touched issue
    report = ed.push_to_github(updated, touched[:1])
    assert report["pushed"] == 0
    assert report["failed"] == 1
    assert "gh issue edit" in report["failures"][0]
    print("test_push_to_github_reports_gh_failure: OK")


# ---------- I/O ----------


def test_load_and_write_enriched_roundtrip() -> None:
    tmp = _TMP_DIR / "enriched.json"
    payload = {"repo": "t/r", "issues": []}
    ed.write_enriched(payload, tmp)
    assert tmp.exists()
    loaded = ed.load_enriched(tmp)
    assert loaded == payload
    print("test_load_and_write_enriched_roundtrip: OK")


def test_load_enriched_errors_when_missing() -> None:
    tmp = _TMP_DIR / "absent.json"
    if tmp.exists():
        tmp.unlink()
    try:
        ed.load_enriched(tmp)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "sync_issues.py" in str(exc) or "heuristics.py" in str(exc)
    print("test_load_enriched_errors_when_missing: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_render_body_block_includes_only_set_fields,
        test_render_body_block_skips_empty_values,
        test_upsert_body_block_appends_when_absent,
        test_upsert_body_block_replaces_when_present,
        test_upsert_body_block_handles_empty_body,
        test_estimate_in_place_fills_missing_dates,
        test_estimate_in_place_skips_already_populated,
        test_estimate_in_place_is_idempotent,
        test_estimate_in_place_sets_estimated_at_timestamp,
        test_push_to_github_edits_only_touched_issues,
        test_push_to_github_preserves_original_body,
        test_push_to_github_reports_gh_failure,
        test_load_and_write_enriched_roundtrip,
        test_load_enriched_errors_when_missing,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} estimate_dates tests passed.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
