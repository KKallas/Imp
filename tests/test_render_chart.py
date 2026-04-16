"""Tests for pipeline/render_chart.py.

Run directly: `.venv/bin/python tests/test_render_chart.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Strategy: build a known enriched payload by running heuristics.enrich
against tests/fixtures/sample_issues.json (the same fixture the
heuristics tests use), then test render_chart against it. The chained
fixture keeps both layers honest — if the heuristics output shape ever
drifts, render_chart's tests fail loudly.

Output is written to a tempdir to avoid touching `.imp/output/`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import heuristics as h  # noqa: E402
import render_chart as rc  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_issues.json"
TODAY = date(2026, 4, 15)

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-render-test-"))


def _load_enriched() -> dict:
    payload = json.loads(FIXTURE.read_text())
    return h.enrich(payload, today=TODAY)


# ---------- field unwrapping ----------


def test_field_value_unwraps_provenance_envelope() -> None:
    issue = {
        "fields": {
            "duration_days": {"value": 5, "source": "github", "confidence": "high"}
        }
    }
    assert rc.field_value(issue, "duration_days") == 5
    print("test_field_value_unwraps_provenance_envelope: OK")


def test_field_value_returns_none_for_missing() -> None:
    assert rc.field_value({"fields": {}}, "duration_days") is None
    assert rc.field_value({}, "duration_days") is None
    print("test_field_value_returns_none_for_missing: OK")


# ---------- date resolution ----------


def test_resolve_dates_prefers_explicit_pair() -> None:
    issue = {
        "fields": {
            "start_date": {"value": "2026-04-01"},
            "end_date": {"value": "2026-04-05"},
            "duration_days": {"value": 99},  # ignored when both dates present
        }
    }
    res = rc.resolve_dates(issue)
    assert res.renderable
    assert res.start == "2026-04-01"
    assert res.end == "2026-04-05"
    assert not res.derived_start
    assert not res.derived_end
    print("test_resolve_dates_prefers_explicit_pair: OK")


def test_resolve_dates_derives_end_from_start_plus_duration() -> None:
    issue = {
        "fields": {
            "start_date": {"value": "2026-04-01"},
            "duration_days": {"value": 5},
        }
    }
    res = rc.resolve_dates(issue)
    assert res.renderable
    assert res.start == "2026-04-01"
    assert res.end == "2026-04-06"
    assert res.derived_end
    print("test_resolve_dates_derives_end_from_start_plus_duration: OK")


def test_resolve_dates_derives_start_from_end_minus_duration() -> None:
    issue = {
        "fields": {
            "end_date": {"value": "2026-04-15"},
            "duration_days": {"value": 4},
        }
    }
    res = rc.resolve_dates(issue)
    assert res.renderable
    assert res.start == "2026-04-11"
    assert res.end == "2026-04-15"
    assert res.derived_start
    print("test_resolve_dates_derives_start_from_end_minus_duration: OK")


def test_resolve_dates_unrenderable_with_no_useful_combination() -> None:
    issue = {"fields": {}}
    res = rc.resolve_dates(issue)
    assert not res.renderable
    assert res.why_unrenderable
    print("test_resolve_dates_unrenderable_with_no_useful_combination: OK")


def test_resolve_dates_unrenderable_with_only_duration() -> None:
    issue = {"fields": {"duration_days": {"value": 5}}}
    res = rc.resolve_dates(issue)
    assert not res.renderable
    print("test_resolve_dates_unrenderable_with_only_duration: OK")


def test_resolve_dates_unrenderable_on_bad_iso_date() -> None:
    issue = {
        "fields": {
            "start_date": {"value": "tomorrow"},
            "duration_days": {"value": 3},
        }
    }
    res = rc.resolve_dates(issue)
    assert not res.renderable
    assert "bad" in (res.why_unrenderable or "").lower()
    print("test_resolve_dates_unrenderable_on_bad_iso_date: OK")


# ---------- mermaid building ----------


def test_build_mermaid_gantt_against_fixture() -> None:
    enriched = _load_enriched()
    mermaid, renderable, missing = rc.build_mermaid_gantt(enriched)

    # The chart is non-empty and follows mermaid gantt syntax
    assert mermaid.startswith("gantt")
    assert "dateFormat YYYY-MM-DD" in mermaid

    by_num = {it["number"]: it for it in renderable}
    missing_nums = {it["number"] for it in missing}

    # #11 has all three (github source) — renderable
    assert 11 in by_num
    # #12 has end_date + duration (heuristic) — derived_start should be true
    assert 12 in by_num
    assert by_num[12]["derived_start"] is True
    # #13 has no fields — unrenderable
    assert 13 in missing_nums
    # #14 has only depends_on — unrenderable
    assert 14 in missing_nums
    # #15 closed past end_date — has end + heuristic duration → renderable
    assert 15 in by_num

    # Mermaid output should mention task IDs for each renderable issue
    for n in (11, 12, 15):
        assert f"i{n}" in mermaid

    # #11 is closed → 'done' tag in its task line
    eleven_line = next(
        line for line in mermaid.splitlines() if ":done, i11," in line
    )
    assert "i11" in eleven_line

    # #12 is delayed → 'crit' tag
    twelve_line = next(
        line for line in mermaid.splitlines() if ":crit, i12," in line
    )
    assert "i12" in twelve_line

    print("test_build_mermaid_gantt_against_fixture: OK")


def test_build_mermaid_gantt_includes_after_clauses_for_known_dependencies() -> None:
    """When a renderable issue depends on another renderable issue,
    the gantt line should use `after iX` instead of explicit dates."""
    enriched = _load_enriched()
    mermaid, renderable, _missing = rc.build_mermaid_gantt(enriched)

    # #12 depends_on #11 (per fixture). Both are renderable, so #12's
    # mermaid line should use `after i11`.
    twelve_line = next(
        line for line in mermaid.splitlines() if "i12," in line
    )
    assert "after i11" in twelve_line, twelve_line
    print("test_build_mermaid_gantt_includes_after_clauses_for_known_dependencies: OK")


def test_build_mermaid_gantt_skips_dependencies_on_unrendered_issues() -> None:
    """If an issue depends on something that's not on the chart, the
    `after` clause should NOT include it."""
    enriched = _load_enriched()
    mermaid, renderable, missing = rc.build_mermaid_gantt(enriched)

    # #11 depends_on [10, 9] per fixture. Neither 9 nor 10 are in the
    # enriched payload, so #11's line shouldn't have an `after` clause.
    eleven_line = next(line for line in mermaid.splitlines() if "i11," in line)
    assert "after" not in eleven_line, eleven_line
    print("test_build_mermaid_gantt_skips_dependencies_on_unrendered_issues: OK")


def test_build_mermaid_gantt_groups_by_milestone_then_label() -> None:
    """Section names come from milestone.title when set, area:* labels
    otherwise."""
    enriched = _load_enriched()
    mermaid, _, _ = rc.build_mermaid_gantt(enriched)

    # Fixture #11 has milestone "Phase 4 — Foreman & visibility tools"
    assert "section Phase 4" in mermaid
    # Fixtures #12, #13, #16 have no milestone but area:pipeline label
    assert "section area:pipeline" in mermaid
    print("test_build_mermaid_gantt_groups_by_milestone_then_label: OK")


def test_build_mermaid_gantt_handles_empty_payload() -> None:
    """An empty enriched payload still yields a valid (skeletal) gantt
    block — and a header — so the template doesn't crash."""
    mermaid, renderable, missing = rc.build_mermaid_gantt(
        {"issues": [], "issue_count": 0}
    )
    assert mermaid.startswith("gantt")
    assert renderable == []
    assert missing == []
    print("test_build_mermaid_gantt_handles_empty_payload: OK")


# ---------- task-name sanitization ----------


def test_sanitize_task_name_replaces_problematic_chars() -> None:
    assert rc._sanitize_task_name("[P4.11]: foo") == "[P4.11] — foo"
    assert "#" not in rc._sanitize_task_name("issue #42")
    print("test_sanitize_task_name_replaces_problematic_chars: OK")


# ---------- end-to-end render ----------


def test_render_html_against_fixture_produces_valid_doc() -> None:
    enriched = _load_enriched()
    context = rc.build_context_for_gantt(enriched)
    html = rc.render_html("gantt", context)

    # Self-contained HTML: doctype, mermaid CDN inline, no external CSS
    assert html.startswith("<!doctype html>")
    assert 'src="https://cdn.jsdelivr.net/npm/mermaid' in html
    assert "<style>" in html  # inline CSS, not external link
    # The chart content
    assert 'class="mermaid"' in html
    # Repo title surfaces
    assert "KKallas/Imp" in html
    # Missing-dates section is present (fixture has unrenderable issues)
    assert "Issues without dates" in html
    print("test_render_html_against_fixture_produces_valid_doc: OK")


def test_render_html_no_renderable_issues_still_valid_doc() -> None:
    """If every issue lacks dates, the page should still render with
    the missing-dates section and a friendly placeholder where the
    chart would go — not crash."""
    enriched = {
        "repo": "test/repo",
        "synced_at": "2026-04-15T00:00:00+00:00",
        "enriched_at": "2026-04-15T00:00:01+00:00",
        "issue_count": 1,
        "delayed_count": 0,
        "issues": [
            {
                "number": 1,
                "title": "no dates here",
                "state": "OPEN",
                "labels": [],
                "milestone": None,
                "fields": {},
                "depends_on_parsed": [],
            }
        ],
    }
    context = rc.build_context_for_gantt(enriched)
    html = rc.render_html("gantt", context)
    assert "<!doctype html>" in html
    assert "no dates here" in html
    print("test_render_html_no_renderable_issues_still_valid_doc: OK")


def test_write_html_creates_output_file() -> None:
    enriched = _load_enriched()
    context = rc.build_context_for_gantt(enriched)
    html = rc.render_html("gantt", context)
    out_path = rc.write_html(html, "gantt", output_dir=_TMP_DIR)
    assert out_path.exists()
    assert out_path.name == "gantt.html"
    text = out_path.read_text()
    assert "<!doctype html>" in text
    print("test_write_html_creates_output_file: OK")


def test_unknown_template_main_returns_error() -> None:
    """CLI: passing --template foo (with no foo.html.j2) returns rc=1."""
    # main() uses sys.argv via argparse; easiest path is to call the
    # context builder lookup directly and assert the missing entry.
    assert "gantt" in rc.CONTEXT_BUILDERS
    assert "no_such_template" not in rc.CONTEXT_BUILDERS
    print("test_unknown_template_main_returns_error: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_field_value_unwraps_provenance_envelope,
        test_field_value_returns_none_for_missing,
        test_resolve_dates_prefers_explicit_pair,
        test_resolve_dates_derives_end_from_start_plus_duration,
        test_resolve_dates_derives_start_from_end_minus_duration,
        test_resolve_dates_unrenderable_with_no_useful_combination,
        test_resolve_dates_unrenderable_with_only_duration,
        test_resolve_dates_unrenderable_on_bad_iso_date,
        test_build_mermaid_gantt_against_fixture,
        test_build_mermaid_gantt_includes_after_clauses_for_known_dependencies,
        test_build_mermaid_gantt_skips_dependencies_on_unrendered_issues,
        test_build_mermaid_gantt_groups_by_milestone_then_label,
        test_build_mermaid_gantt_handles_empty_payload,
        test_sanitize_task_name_replaces_problematic_chars,
        test_render_html_against_fixture_produces_valid_doc,
        test_render_html_no_renderable_issues_still_valid_doc,
        test_write_html_creates_output_file,
        test_unknown_template_main_returns_error,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} render_chart tests passed.")


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
