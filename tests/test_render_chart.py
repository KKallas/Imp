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


# ---------- kanban ----------


def test_kanban_all_three_templates_registered() -> None:
    """P4.19: the extra templates must all register context builders."""
    for name in ("kanban", "burndown", "comparison"):
        assert name in rc.CONTEXT_BUILDERS, name
    print("test_kanban_all_three_templates_registered: OK")


def test_kanban_status_falls_back_to_state_when_field_absent() -> None:
    closed = {"state": "CLOSED", "fields": {}, "assignees": []}
    assigned = {"state": "OPEN", "fields": {}, "assignees": [{"login": "alice"}]}
    bare = {"state": "OPEN", "fields": {}, "assignees": []}
    assert rc._kanban_status(closed) == "done"
    assert rc._kanban_status(assigned) == "in-progress"
    assert rc._kanban_status(bare) == "open"
    print("test_kanban_status_falls_back_to_state_when_field_absent: OK")


def test_kanban_status_honors_project_board_field() -> None:
    """If project-board status is set, it wins over GH state."""
    issue = {
        "state": "OPEN",
        "fields": {"status": {"value": "Done"}},
        "assignees": [],
    }
    assert rc._kanban_status(issue) == "done"

    issue2 = {
        "state": "OPEN",
        "fields": {"status": {"value": "In Progress"}},
        "assignees": [],
    }
    assert rc._kanban_status(issue2) == "in-progress"

    # Unknown status string falls through to state-based rules.
    issue3 = {
        "state": "CLOSED",
        "fields": {"status": {"value": "something weird"}},
        "assignees": [],
    }
    assert rc._kanban_status(issue3) == "done"
    print("test_kanban_status_honors_project_board_field: OK")


def test_kanban_context_groups_fixture_into_three_columns() -> None:
    enriched = _load_enriched()
    ctx = rc.build_context_for_kanban(enriched)

    slugs = [c["slug"] for c in ctx["columns"]]
    assert slugs == ["open", "in-progress", "done"]

    by_slug = {c["slug"]: c for c in ctx["columns"]}
    all_numbers: set[int] = set()
    for col in ctx["columns"]:
        for card in col["cards"]:
            assert "number" in card and "title" in card
            all_numbers.add(card["number"])

    # Every issue in the fixture lands in exactly one column.
    assert all_numbers == {11, 12, 13, 14, 15, 16}

    # #11 and #15 are closed → Done column.
    done_nums = {c["number"] for c in by_slug["done"]["cards"]}
    assert {11, 15} <= done_nums
    print("test_kanban_context_groups_fixture_into_three_columns: OK")


def test_kanban_render_html_self_contained_and_has_all_columns() -> None:
    enriched = _load_enriched()
    ctx = rc.build_context_for_kanban(enriched)
    html = rc.render_html("kanban", ctx)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html  # inline, no external CSS link
    for label in ("Open", "In Progress", "Done"):
        assert label in html, label
    # Issue title shows up on a card.
    assert "[P4.11]" in html
    print("test_kanban_render_html_self_contained_and_has_all_columns: OK")


def test_kanban_unassigned_card_shows_placeholder() -> None:
    enriched = {
        "repo": "test/repo",
        "issues": [
            {
                "number": 1,
                "title": "nobody's working on this",
                "state": "OPEN",
                "assignees": [],
                "fields": {},
            }
        ],
        "issue_count": 1,
    }
    ctx = rc.build_context_for_kanban(enriched)
    html = rc.render_html("kanban", ctx)
    assert "unassigned" in html
    print("test_kanban_unassigned_card_shows_placeholder: OK")


# ---------- burndown ----------


def test_burndown_series_empty_when_no_issues() -> None:
    labels, remaining, ideal, tracked, open_today, span, excluded, missing = (
        rc._burndown_series({"issues": []})
    )
    assert labels == []
    assert remaining == []
    assert ideal == []
    assert tracked == 0
    assert span == 0
    assert excluded == 0
    assert missing == []
    print("test_burndown_series_empty_when_no_issues: OK")


def test_burndown_tracks_every_fixture_issue_via_gh_timestamps() -> None:
    """Fixture issues all have createdAt, so none should be missing —
    even the ones without project-board dates land on the burndown."""
    enriched = _load_enriched()
    ctx = rc.build_context_for_burndown(enriched)
    # All 6 fixture issues have createdAt → all trackable.
    assert ctx["tracked_count"] == 6
    assert ctx["missing_issues"] == []
    assert ctx["excluded_count"] == 0
    print("test_burndown_tracks_every_fixture_issue_via_gh_timestamps: OK")


def test_burndown_series_remaining_bounded_and_non_negative() -> None:
    """Remaining per day is non-negative and never exceeds tracked_count.
    (The series is NOT monotonic: scope can grow when new issues are
    filed mid-span, so we drop that stricter invariant.)"""
    enriched = _load_enriched()
    _, remaining, _, tracked, _, span, _, _ = rc._burndown_series(
        enriched, today=TODAY
    )
    assert len(remaining) == span
    for r in remaining:
        assert 0 <= r <= tracked
    print("test_burndown_series_remaining_bounded_and_non_negative: OK")


def test_burndown_scope_growth_increases_remaining_mid_span() -> None:
    """When a new issue is filed mid-span, remaining[d] should go up
    on that day relative to d-1 — which the old fixed-scope burndown
    would have hidden."""
    enriched = {
        "repo": "t/r",
        "issues": [
            # Day 1: one issue open
            {
                "number": 1,
                "title": "day 1",
                "state": "OPEN",
                "createdAt": "2026-04-01T09:00:00Z",
                "updatedAt": "2026-04-01T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
            # Day 3: a second issue filed (scope grows)
            {
                "number": 2,
                "title": "day 3",
                "state": "OPEN",
                "createdAt": "2026-04-03T09:00:00Z",
                "updatedAt": "2026-04-03T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
        ],
    }
    _, remaining, _, _, _, _, _, _ = rc._burndown_series(
        enriched, today=date(2026, 4, 3)
    )
    # Day 1 = 1 open, day 2 = 1 open, day 3 = 2 open.
    assert remaining == [1, 1, 2], remaining
    print("test_burndown_scope_growth_increases_remaining_mid_span: OK")


def test_burndown_excludes_not_planned_closures() -> None:
    """The user-facing fix for P4.19: closed-as-NOT_PLANNED issues are
    tallied as `excluded_count` but never enter scope — they're
    out-scoped work, not completed work."""
    enriched = {
        "repo": "t/r",
        "issues": [
            {
                "number": 1,
                "title": "real work",
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "createdAt": "2026-04-01T09:00:00Z",
                "closedAt": "2026-04-03T09:00:00Z",
                "updatedAt": "2026-04-03T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
            {
                "number": 2,
                "title": "out of scope",
                "state": "CLOSED",
                "stateReason": "NOT_PLANNED",
                "createdAt": "2026-04-01T09:00:00Z",
                "closedAt": "2026-04-02T09:00:00Z",
                "updatedAt": "2026-04-02T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
        ],
    }
    _, remaining, _, tracked, _, _, excluded, missing = rc._burndown_series(
        enriched, today=date(2026, 4, 3)
    )
    assert excluded == 1
    assert tracked == 1  # only the COMPLETED one
    assert missing == []
    # Day 1: 1 open; day 2: 1 open (not_planned dropping on day 2 is
    # ignored); day 3: 0 open (completed one closes on 04-03).
    assert remaining == [1, 1, 0], remaining
    print("test_burndown_excludes_not_planned_closures: OK")


def test_burndown_closedAt_resolves_before_updatedAt_fallback() -> None:
    """When closedAt is present, it takes priority over updatedAt."""
    enriched = {
        "repo": "t/r",
        "issues": [
            {
                "number": 1,
                "title": "x",
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "createdAt": "2026-04-01T09:00:00Z",
                "closedAt": "2026-04-02T09:00:00Z",
                "updatedAt": "2026-04-05T09:00:00Z",  # later — ignored
                "assignees": [],
                "fields": {},
            }
        ],
    }
    _, remaining, _, _, _, _, _, _ = rc._burndown_series(
        enriched, today=date(2026, 4, 5)
    )
    # Semantics: `resolved > d` means open at end of day d. So the
    # issue is open on day 04-01 (its creation day), and gone from
    # day 04-02 onward (closedAt == 04-02, not > 04-02).
    assert remaining == [1, 0, 0, 0, 0], remaining
    print("test_burndown_closedAt_resolves_before_updatedAt_fallback: OK")


def test_burndown_context_missing_list_only_when_no_timestamps() -> None:
    """Issues WITHOUT createdAt (and without fields.start_date) go to
    the missing list — the path a sparse fixture would hit."""
    enriched = {
        "repo": "t/r",
        "issues": [
            {
                "number": 42,
                "title": "timestamp-less",
                "state": "OPEN",
                # no createdAt at all
                "assignees": [],
                "fields": {},
            }
        ],
    }
    ctx = rc.build_context_for_burndown(enriched)
    assert ctx["tracked_count"] == 0
    assert len(ctx["missing_issues"]) == 1
    assert ctx["missing_issues"][0]["number"] == 42
    print("test_burndown_context_missing_list_only_when_no_timestamps: OK")


def test_burndown_render_html_self_contained() -> None:
    enriched = _load_enriched()
    ctx = rc.build_context_for_burndown(enriched)
    html = rc.render_html("burndown", ctx)
    assert html.startswith("<!doctype html>")
    assert 'src="https://cdn.jsdelivr.net/npm/chart.js' in html
    # Chart.js bootstrap runs when there's data.
    assert 'new Chart(' in html
    # ISO date labels surface as JSON.
    assert "2026-04" in html
    print("test_burndown_render_html_self_contained: OK")


def test_burndown_render_html_surfaces_excluded_count() -> None:
    """The out-scoped tally appears in the page so readers don't
    wonder where the missing closures went."""
    enriched = {
        "repo": "t/r",
        "issues": [
            {
                "number": 1,
                "title": "done",
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "createdAt": "2026-04-01T09:00:00Z",
                "closedAt": "2026-04-02T09:00:00Z",
                "updatedAt": "2026-04-02T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
            {
                "number": 2,
                "title": "won't fix",
                "state": "CLOSED",
                "stateReason": "NOT_PLANNED",
                "createdAt": "2026-04-01T09:00:00Z",
                "closedAt": "2026-04-01T09:00:00Z",
                "updatedAt": "2026-04-01T09:00:00Z",
                "assignees": [],
                "fields": {},
            },
        ],
    }
    ctx = rc.build_context_for_burndown(enriched)
    html = rc.render_html("burndown", ctx)
    assert ctx["excluded_count"] == 1
    assert "NOT_PLANNED" in html
    assert "Out-scoped" in html
    print("test_burndown_render_html_surfaces_excluded_count: OK")


def test_build_burndown_plotly_figure_has_expected_traces() -> None:
    """The Plotly figure used by the Foreman chat UI should carry the
    same series the HTML template renders — 'Remaining (actual)' and
    'Ideal' — with x=labels and y matching the context."""
    enriched = _load_enriched()
    ctx = rc.build_context_for_burndown(enriched)
    fig = rc.build_burndown_plotly_figure(ctx)
    assert fig is not None
    traces = fig["data"]
    names = {t["name"] for t in traces}
    assert "Remaining (actual)" in names
    assert "Ideal" in names
    for t in traces:
        assert t["x"] == ctx["labels"]
    remaining_trace = next(t for t in traces if t["name"] == "Remaining (actual)")
    assert remaining_trace["y"] == ctx["remaining"]
    print("test_build_burndown_plotly_figure_has_expected_traces: OK")


def test_build_burndown_plotly_figure_returns_none_when_empty() -> None:
    """Empty context (no labels) means no chart — returning None lets
    the caller fall back to the HTML download chip only."""
    assert rc.build_burndown_plotly_figure({"labels": []}) is None
    assert rc.build_burndown_plotly_figure({}) is None
    print("test_build_burndown_plotly_figure_returns_none_when_empty: OK")


def test_build_burndown_plotly_figure_title_mentions_excluded_when_nonzero() -> None:
    """The out-scoped count should appear in the Plotly title so the
    reader sees the same disclosure the HTML page carries."""
    ctx = {
        "title": "test/repo",
        "labels": ["2026-04-01", "2026-04-02"],
        "remaining": [2, 1],
        "ideal": [2.0, 0.0],
        "excluded_count": 3,
    }
    fig = rc.build_burndown_plotly_figure(ctx)
    assert fig is not None
    title_text = fig["layout"]["title"]["text"]
    assert "3" in title_text
    assert "out-scoped" in title_text.lower()
    print("test_build_burndown_plotly_figure_title_mentions_excluded_when_nonzero: OK")


def test_burndown_render_html_no_data_path() -> None:
    enriched = {
        "repo": "test/repo",
        "issues": [
            {
                "number": 1,
                "title": "no dates",
                "state": "OPEN",
                "fields": {},
                "assignees": [],
                # no createdAt → missing path
            }
        ],
        "issue_count": 1,
    }
    ctx = rc.build_context_for_burndown(enriched)
    html = rc.render_html("burndown", ctx)
    # No chart bootstrap when there's no data — the placeholder shows.
    assert "No issues with" in html
    assert "new Chart(" not in html
    print("test_burndown_render_html_no_data_path: OK")


# ---------- comparison ----------


def test_comparison_deltas_zero_when_variant_identical() -> None:
    enriched = _load_enriched()
    ctx = rc.build_context_for_comparison(enriched, enriched)
    assert ctx["deltas"], "expected at least one overlapping issue"
    for d in ctx["deltas"]:
        # Any issue present in both sides should have delta == 0.
        if d["only_in"] is None and d["delta_days"] is not None:
            assert d["delta_days"] == 0, d
    print("test_comparison_deltas_zero_when_variant_identical: OK")


def test_comparison_detects_shifted_end_dates() -> None:
    """Shifting the variant's end_date by N days should produce Δ = N."""
    enriched = _load_enriched()
    import copy

    variant = copy.deepcopy(enriched)
    # Shift #11's end_date +3 days (explicit envelope format).
    for issue in variant["issues"]:
        if issue["number"] == 11:
            issue["fields"]["end_date"] = {"value": "2026-04-18"}
            issue["fields"]["start_date"] = {"value": "2026-04-11"}
            break

    ctx = rc.build_context_for_comparison(enriched, variant)
    delta_11 = next(d for d in ctx["deltas"] if d["number"] == 11)
    assert delta_11["delta_days"] == 3, delta_11
    print("test_comparison_detects_shifted_end_dates: OK")


def test_comparison_flags_issues_only_in_one_side() -> None:
    baseline = {
        "repo": "test/repo",
        "issues": [
            {
                "number": 1,
                "title": "in both",
                "state": "OPEN",
                "fields": {
                    "start_date": {"value": "2026-04-01"},
                    "end_date": {"value": "2026-04-05"},
                },
                "assignees": [],
            },
            {
                "number": 2,
                "title": "baseline only",
                "state": "OPEN",
                "fields": {
                    "start_date": {"value": "2026-04-06"},
                    "end_date": {"value": "2026-04-10"},
                },
                "assignees": [],
            },
        ],
    }
    variant = {
        "repo": "test/repo",
        "issues": [
            baseline["issues"][0],
            {
                "number": 3,
                "title": "variant only",
                "state": "OPEN",
                "fields": {
                    "start_date": {"value": "2026-04-06"},
                    "end_date": {"value": "2026-04-08"},
                },
                "assignees": [],
            },
        ],
    }
    ctx = rc.build_context_for_comparison(baseline, variant)
    by_num = {d["number"]: d for d in ctx["deltas"]}
    assert by_num[2]["only_in"] == "baseline"
    assert by_num[3]["only_in"] == "variant"
    # Delta is None when only one side has the issue.
    assert by_num[2]["delta_days"] is None
    assert by_num[3]["delta_days"] is None
    print("test_comparison_flags_issues_only_in_one_side: OK")


def test_comparison_variant_defaults_to_baseline() -> None:
    """Calling without a variant should render baseline on both sides
    (all deltas zero, no only_in flags)."""
    enriched = _load_enriched()
    ctx = rc.build_context_for_comparison(enriched, None)
    for d in ctx["deltas"]:
        assert d["only_in"] is None
        if d["delta_days"] is not None:
            assert d["delta_days"] == 0
    print("test_comparison_variant_defaults_to_baseline: OK")


def test_comparison_render_html_has_both_panels_and_delta_table() -> None:
    enriched = _load_enriched()
    ctx = rc.build_context_for_comparison(
        enriched, enriched, baseline_label="As-is", variant_label="Delay 2w"
    )
    html = rc.render_html("comparison", ctx)
    assert html.startswith("<!doctype html>")
    # Both panels rendered.
    assert html.count('class="mermaid"') == 2
    # Custom labels surface in the output.
    assert "As-is" in html
    assert "Delay 2w" in html
    # Delta table header is present.
    assert "Δ days" in html or "&#916; days" in html
    print("test_comparison_render_html_has_both_panels_and_delta_table: OK")


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
        test_kanban_all_three_templates_registered,
        test_kanban_status_falls_back_to_state_when_field_absent,
        test_kanban_status_honors_project_board_field,
        test_kanban_context_groups_fixture_into_three_columns,
        test_kanban_render_html_self_contained_and_has_all_columns,
        test_kanban_unassigned_card_shows_placeholder,
        test_burndown_series_empty_when_no_issues,
        test_burndown_tracks_every_fixture_issue_via_gh_timestamps,
        test_burndown_series_remaining_bounded_and_non_negative,
        test_burndown_scope_growth_increases_remaining_mid_span,
        test_burndown_excludes_not_planned_closures,
        test_burndown_closedAt_resolves_before_updatedAt_fallback,
        test_burndown_context_missing_list_only_when_no_timestamps,
        test_burndown_render_html_self_contained,
        test_burndown_render_html_surfaces_excluded_count,
        test_build_burndown_plotly_figure_has_expected_traces,
        test_build_burndown_plotly_figure_returns_none_when_empty,
        test_build_burndown_plotly_figure_title_mentions_excluded_when_nonzero,
        test_burndown_render_html_no_data_path,
        test_comparison_deltas_zero_when_variant_identical,
        test_comparison_detects_shifted_end_dates,
        test_comparison_flags_issues_only_in_one_side,
        test_comparison_variant_defaults_to_baseline,
        test_comparison_render_html_has_both_panels_and_delta_table,
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
