"""Tests for pipeline/scenarios.py.

Run directly: `.venv/bin/python tests/test_scenarios.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Covers:
  - Out collector methods + serialisation shape
  - @scenario decorator + discovery after exec
  - Every filter primitive (delay_all, delay_issue, drop_issue,
    scale_durations, shift_start, exclude_weekends, freeze_after)
  - AST validation (accepts safe sources; rejects forbidden imports,
    exec/eval, dunder access)
  - Session I/O (save / load / run / commit / close)
  - Generator with a fake backend (no live LLM)
  - Active-scenario composition (apply_active_scenario round-trip)

SESSIONS_DIR is redirected to a tempdir so the shared `.imp/scenarios/`
is never touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import scenarios as sc  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-scn-test-"))
sc.SESSIONS_DIR = _TMP_DIR / "scenarios"
sc.ROOT = _TMP_DIR  # so active_scenario.json etc. land in the tempdir


# ---------- fixtures ----------


def _baseline() -> dict:
    return {
        "issues": [
            {
                "number": 11,
                "state": "CLOSED",
                "labels": [{"name": "area:server"}],
                "depends_on_parsed": [],
                "fields": {
                    "duration_days": {"value": 4, "source": "heuristic"},
                    "start_date": {"value": "2026-04-11"},
                    "end_date": {"value": "2026-04-15"},
                },
            },
            {
                "number": 12,
                "state": "OPEN",
                "labels": [{"name": "area:pipeline"}, {"name": "imp:baseline"}],
                "depends_on_parsed": [11],
                "fields": {
                    "duration_days": {"value": 2, "source": "heuristic"},
                    "start_date": {"value": "2026-04-15"},
                    "end_date": {"value": "2026-04-17"},
                },
            },
            {
                "number": 13,
                "state": "OPEN",
                "labels": [{"name": "area:ui"}],
                "depends_on_parsed": [12],
                "fields": {
                    "duration_days": {"value": 3},
                    "start_date": {"value": "2026-04-17"},
                    "end_date": {"value": "2026-04-20"},
                },
            },
        ],
        "issue_count": 3,
    }


# ---------- Out collector ----------


def test_out_metric_list_text_serialize() -> None:
    out = sc.Out(name="x")
    out.metric("duration", "35d")
    out.list("blockers", [1, 2, 3])
    out.text("note", "hello")
    d = out.to_dict()
    assert d["name"] == "x"
    assert d["metrics"] == [("duration", "35d")]
    assert d["lists"] == [["blockers", ["1", "2", "3"]]]
    assert d["texts"] == [("note", "hello")]
    print("test_out_metric_list_text_serialize: OK")


def test_out_chart_accepts_dict() -> None:
    out = sc.Out(name="x")
    fig = {"data": [{"type": "bar"}], "layout": {"title": "t"}}
    out.chart(fig)
    assert out.charts == [fig]
    print("test_out_chart_accepts_dict: OK")


def test_out_chart_rejects_wrong_type() -> None:
    out = sc.Out(name="x")
    try:
        out.chart("not a figure")  # type: ignore[arg-type]
    except TypeError:
        print("test_out_chart_rejects_wrong_type: OK")
        return
    assert False, "expected TypeError"


# ---------- @scenario decorator ----------


def test_scenario_decorator_attaches_name() -> None:
    @sc.scenario("my scenario")
    def fn(data, out):
        pass

    assert getattr(fn, "_scenario_name") == "my scenario"
    print("test_scenario_decorator_attaches_name: OK")


def test_scenario_decorator_rejects_empty_name() -> None:
    try:
        sc.scenario("")
    except ValueError:
        print("test_scenario_decorator_rejects_empty_name: OK")
        return
    assert False, "expected ValueError"


# ---------- filter primitives ----------


def test_delay_all_shifts_both_dates() -> None:
    out = sc.delay_all(_baseline(), 7)
    i11 = next(i for i in out["issues"] if i["number"] == 11)
    assert i11["fields"]["start_date"]["value"] == "2026-04-18"
    assert i11["fields"]["end_date"]["value"] == "2026-04-22"
    print("test_delay_all_shifts_both_dates: OK")


def test_delay_all_does_not_mutate_input() -> None:
    base = _baseline()
    snap = json.dumps(base, sort_keys=True)
    sc.delay_all(base, 14)
    assert json.dumps(base, sort_keys=True) == snap
    print("test_delay_all_does_not_mutate_input: OK")


def test_delay_issue_cascades_to_dependents() -> None:
    out = sc.delay_issue(_baseline(), 11, 5)
    by_num = {i["number"]: i for i in out["issues"]}
    assert by_num[11]["fields"]["end_date"]["value"] == "2026-04-20"
    # Issue 12 depends on 11 — its start moves to 11's new end
    assert by_num[12]["fields"]["start_date"]["value"] >= "2026-04-20"
    print("test_delay_issue_cascades_to_dependents: OK")


def test_drop_issue_removes_and_prunes_deps() -> None:
    out = sc.drop_issue(_baseline(), 11)
    assert all(i["number"] != 11 for i in out["issues"])
    assert out["issue_count"] == 2
    # Issue 12 had 11 as a dep — should be pruned
    twelve = next(i for i in out["issues"] if i["number"] == 12)
    assert 11 not in (twelve.get("depends_on_parsed") or [])
    print("test_drop_issue_removes_and_prunes_deps: OK")


def test_scale_durations_scales_and_recomputes_end() -> None:
    out = sc.scale_durations(_baseline(), 2.0)
    i11 = next(i for i in out["issues"] if i["number"] == 11)
    assert i11["fields"]["duration_days"]["value"] == 8
    # end = start + new duration
    assert i11["fields"]["end_date"]["value"] == "2026-04-19"
    print("test_scale_durations_scales_and_recomputes_end: OK")


def test_scale_durations_where_filter() -> None:
    out = sc.scale_durations(_baseline(), 3.0, where={"label": "area:server"})
    by_num = {i["number"]: i for i in out["issues"]}
    # Only #11 has area:server
    assert by_num[11]["fields"]["duration_days"]["value"] == 12
    # Others unchanged
    assert by_num[12]["fields"]["duration_days"]["value"] == 2
    print("test_scale_durations_where_filter: OK")


def test_scale_durations_rejects_non_positive() -> None:
    try:
        sc.scale_durations(_baseline(), 0)
    except ValueError:
        print("test_scale_durations_rejects_non_positive: OK")
        return
    assert False, "expected ValueError"


def test_shift_start_anchors_to_new_date() -> None:
    out = sc.shift_start(_baseline(), "2026-05-01")
    i11 = next(i for i in out["issues"] if i["number"] == 11)
    # 11 was earliest at 2026-04-11 → shifts to 2026-05-01 (delta +20 days)
    assert i11["fields"]["start_date"]["value"] == "2026-05-01"
    print("test_shift_start_anchors_to_new_date: OK")


def test_exclude_weekends_stretches_end_dates() -> None:
    out = sc.exclude_weekends(_baseline())
    i11 = next(i for i in out["issues"] if i["number"] == 11)
    # duration 4 → 4*1.4 = 5.6 → round = 6
    assert i11["fields"]["duration_days"]["value"] == 6
    print("test_exclude_weekends_stretches_end_dates: OK")


def test_freeze_after_drops_later_issues() -> None:
    out = sc.freeze_after(_baseline(), "2026-04-16")
    # Issues with start_date > 2026-04-16 should be dropped
    # #13 starts at 2026-04-17 → dropped
    # #11, #12 start on/before → kept
    nums = {i["number"] for i in out["issues"]}
    assert 13 not in nums
    assert {11, 12} <= nums
    print("test_freeze_after_drops_later_issues: OK")


# ---------- AST validation ----------


def test_validator_accepts_safe_source() -> None:
    src = """
from datetime import date, timedelta

@scenario("x")
def s(data, out):
    out.metric("count", len(data["issues"]))
    return data
"""
    sc._validate_scenarios_source(src)  # should not raise
    print("test_validator_accepts_safe_source: OK")


def test_validator_rejects_os_import() -> None:
    src = """
import os

@scenario("bad")
def s(data, out):
    pass
"""
    try:
        sc._validate_scenarios_source(src)
    except sc.ScenarioValidationError as exc:
        assert "os" in str(exc)
        print("test_validator_rejects_os_import: OK")
        return
    assert False, "expected ScenarioValidationError"


def test_validator_rejects_exec_call() -> None:
    src = """
@scenario("bad")
def s(data, out):
    exec("print(1)")
"""
    try:
        sc._validate_scenarios_source(src)
    except sc.ScenarioValidationError as exc:
        assert "exec" in str(exc)
        print("test_validator_rejects_exec_call: OK")
        return
    assert False, "expected ScenarioValidationError"


def test_validator_rejects_dunder_access() -> None:
    src = """
@scenario("bad")
def s(data, out):
    x = data.__class__
"""
    try:
        sc._validate_scenarios_source(src)
    except sc.ScenarioValidationError as exc:
        assert "dunder" in str(exc).lower()
        print("test_validator_rejects_dunder_access: OK")
        return
    assert False, "expected ScenarioValidationError"


# ---------- session lifecycle ----------


async def test_start_session_with_fake_generator() -> None:
    """End-to-end: fake generator produces a valid scenarios.py, session
    is saved, run_session returns one Out per scenario."""

    async def fake_gen(descriptions):
        # Emit a dead-simple two-scenario file that references the API
        return """
@scenario("as-is")
def s1(data, out):
    out.metric("count", len(data["issues"]))
    return data

@scenario("all dropped")
def s2(data, out):
    remaining = drop_issue(data, 11)
    remaining = drop_issue(remaining, 12)
    out.metric("count", len(remaining["issues"]))
    return remaining
"""

    sc.set_generator_backend(fake_gen)
    try:
        session_id, outs = await sc.start_session(
            ["as-is", "all dropped"], _baseline()
        )
    finally:
        sc.set_generator_backend(None)

    assert session_id.startswith("scn-")
    assert len(outs) == 2
    assert outs[0].name == "as-is"
    assert outs[1].name == "all dropped"
    # Second scenario dropped 2 issues → count = 1
    second_count = dict(outs[1].metrics).get("count")
    assert second_count == "1"
    # Session files on disk
    d = sc.session_dir(session_id)
    assert (d / "scenarios.py").exists()
    assert (d / "descriptions.txt").exists()
    assert (d / "result.json").exists()
    print("test_start_session_with_fake_generator: OK")


async def test_commit_and_close_session() -> None:
    async def fake_gen(descriptions):
        return """
@scenario("a")
def s1(data, out):
    out.metric("id", "a")
    return data

@scenario("b")
def s2(data, out):
    out.metric("id", "b")
    return data
"""

    sc.set_generator_backend(fake_gen)
    try:
        session_id, _ = await sc.start_session(["a", "b"], _baseline())
    finally:
        sc.set_generator_backend(None)

    # Commit scenario index 1
    committed = sc.commit_session(session_id, 1, _baseline())
    assert committed["choice_index"] == 1
    assert committed["choice_name"] == "b"
    commit_file = sc.session_dir(session_id) / "committed.json"
    assert commit_file.exists()

    # Active pointer is set
    active = sc.active_session()
    assert active["session_id"] == session_id
    assert active["choice_index"] == 1

    # Close (after commit): active pointer cleared if this session was active
    sc.close_session(session_id)
    assert sc.active_session() is None
    print("test_commit_and_close_session: OK")


async def test_commit_out_of_range_rejected() -> None:
    async def fake_gen(descriptions):
        return """
@scenario("only")
def s1(data, out):
    return data
"""

    sc.set_generator_backend(fake_gen)
    try:
        session_id, _ = await sc.start_session(["only", "second"], _baseline())
    finally:
        sc.set_generator_backend(None)

    try:
        sc.commit_session(session_id, 99, _baseline())
    except ValueError as exc:
        assert "out of range" in str(exc)
        print("test_commit_out_of_range_rejected: OK")
        return
    assert False, "expected ValueError on bad choice_index"


async def test_list_sessions_newest_first() -> None:
    async def fake_gen(descriptions):
        return """
@scenario("x")
def s1(data, out):
    return data

@scenario("y")
def s2(data, out):
    return data
"""

    sc.set_generator_backend(fake_gen)
    try:
        first, _ = await sc.start_session(["x", "y"], _baseline())
        # Session IDs encode seconds; force a tick-over so second > first
        # in sort order regardless of the random token tiebreaker.
        await asyncio.sleep(1.1)
        second, _ = await sc.start_session(["x", "y"], _baseline())
    finally:
        sc.set_generator_backend(None)

    rows = sc.list_sessions()
    assert rows[0]["session_id"] == second
    print("test_list_sessions_newest_first: OK")


# ---------- active-scenario composition ----------


async def test_apply_active_scenario_composes() -> None:
    """After commit, apply_active_scenario applies the committed
    function's transformation to baseline data."""

    async def fake_gen(descriptions):
        return """
@scenario("delay 10")
def s1(data, out):
    return delay_all(data, 10)

@scenario("no-op")
def s2(data, out):
    return data
"""

    sc.set_generator_backend(fake_gen)
    try:
        session_id, _ = await sc.start_session(["delay 10", "no-op"], _baseline())
    finally:
        sc.set_generator_backend(None)

    # Commit scenario 0 (delay 10)
    sc.commit_session(session_id, 0, _baseline())

    composed = sc.apply_active_scenario(_baseline())
    i11 = next(i for i in composed["issues"] if i["number"] == 11)
    # Baseline #11 start was 2026-04-11; delay 10 → 2026-04-21
    assert i11["fields"]["start_date"]["value"] == "2026-04-21"
    print("test_apply_active_scenario_composes: OK")


async def test_apply_active_scenario_noop_when_no_commit() -> None:
    # Clear any active pointer from prior tests
    active_ptr = sc.ROOT / ".imp" / "active_scenario.json"
    if active_ptr.exists():
        active_ptr.unlink()
    composed = sc.apply_active_scenario(_baseline())
    assert composed == _baseline()
    print("test_apply_active_scenario_noop_when_no_commit: OK")


# ---------- generator guards ----------


async def test_start_session_rejects_too_few_or_too_many() -> None:
    async def fake_gen(descriptions):
        return "@scenario('a')\ndef s1(data,out): return data"

    sc.set_generator_backend(fake_gen)
    try:
        try:
            await sc.start_session(["only one"], _baseline())
        except ValueError as exc:
            assert "at least" in str(exc).lower()
        else:
            assert False, "expected ValueError on <2 scenarios"

        try:
            await sc.start_session([str(i) for i in range(10)], _baseline())
        except ValueError as exc:
            assert "max" in str(exc).lower()
        else:
            assert False, "expected ValueError on >5 scenarios"
    finally:
        sc.set_generator_backend(None)
    print("test_start_session_rejects_too_few_or_too_many: OK")


def test_build_gantt_figure_labels_use_short_format() -> None:
    """Issue labels on the gantt are compact (`#42 [P4.16]` or `#42`)
    so they don't stack on top of each other on narrow screens. Full
    titles are tooltip material, not y-axis material."""
    data = sc.synthesize_dates({
        "issues": [
            {"number": 16, "state": "OPEN",
             "title": "[P4.16] pipeline/scenarios — compare N scenarios",
             "labels": [], "depends_on_parsed": [],
             "fields": {"duration_days": {"value": 2}}},
            {"number": 42, "state": "OPEN", "title": "no phase tag",
             "labels": [], "depends_on_parsed": [],
             "fields": {"duration_days": {"value": 1}}},
        ]
    }, today=date(2026, 4, 15))
    fig = sc.build_gantt_figure(data)
    labels = fig["data"][0]["y"]
    assert labels == ["#16 [P4.16]", "#42"], labels
    print("test_build_gantt_figure_labels_use_short_format: OK")


def test_short_issue_label_recognises_various_phase_tags() -> None:
    cases = [
        ({"number": 1, "title": "[P4.16] foo"},   "#1 [P4.16]"),
        ({"number": 2, "title": "[P2.9b] bar"},   "#2 [P2.9b]"),
        ({"number": 3, "title": "[P10] baz"},     "#3 [P10]"),
        ({"number": 4, "title": "no tag at all"}, "#4"),
        ({"number": 5, "title": ""},              "#5"),
        ({"title": "[P1] headless"},              "#? [P1]"),
    ]
    for issue, expected in cases:
        got = sc._short_issue_label(issue)
        assert got == expected, f"{issue!r} → {got!r}, expected {expected!r}"
    print("test_short_issue_label_recognises_various_phase_tags: OK")


def test_build_gantt_figure_scales_to_milliseconds() -> None:
    """Plotly's date-type x-axis requires bar widths in ms. The LLM
    was previously emitting day counts directly, rendering bars a few
    ms wide (invisible). Helper must do the conversion."""
    data = sc.synthesize_dates({
        "issues": [
            {"number": 1, "state": "OPEN", "title": "issue 1",
             "labels": [], "depends_on_parsed": [],
             "fields": {"duration_days": {"value": 5},
                        "start_date": {"value": "2026-04-15"},
                        "end_date": {"value": "2026-04-20"}}},
        ]
    })
    fig = sc.build_gantt_figure(data, title="test")
    # 5 days * 86_400_000 ms/day
    assert fig["data"][0]["x"] == [5 * 86_400_000]
    assert fig["layout"]["xaxis"]["type"] == "date"
    print("test_build_gantt_figure_scales_to_milliseconds: OK")


def test_build_gantt_figure_colours_by_state() -> None:
    data = sc.synthesize_dates({
        "issues": [
            {"number": 1, "state": "CLOSED", "title": "done",
             "labels": [], "depends_on_parsed": [],
             "createdAt": "2026-04-11T00:00:00Z",
             "closedAt": "2026-04-15T00:00:00Z",
             "fields": {"duration_days": {"value": 4}}},
            {"number": 2, "state": "OPEN", "title": "todo",
             "labels": [], "depends_on_parsed": [],
             "fields": {"duration_days": {"value": 3}}},
        ]
    }, today=date(2026, 4, 15))
    fig = sc.build_gantt_figure(data)
    colors = fig["data"][0]["marker"]["color"]
    assert colors[0] == "#22c55e"  # closed = green
    assert colors[1] == "#3b82f6"  # open = blue
    print("test_build_gantt_figure_colours_by_state: OK")


def test_build_gantt_figure_skips_undated_issues() -> None:
    """Issues that didn't get dates (edge case: synthesis couldn't
    resolve) are skipped rather than crashing or producing bogus bars."""
    data = {"issues": [{"number": 1, "state": "OPEN", "title": "x",
                        "labels": [], "depends_on_parsed": [],
                        "fields": {}}]}
    fig = sc.build_gantt_figure(data)
    assert fig["data"] == []
    assert "no datable" in fig["layout"]["title"]["text"]
    print("test_build_gantt_figure_skips_undated_issues: OK")


def test_build_gantt_figure_reachable_from_exec_namespace() -> None:
    """Generated scenario source can call the helper directly."""
    src = """
@scenario("gantt")
def s(data, out):
    out.chart(build_gantt_figure(data, title="gantt"))
    return data
"""
    fns = sc._exec_scenarios_source(src)
    out = sc.Out(name=fns[0]._scenario_name)
    baseline = sc.synthesize_dates({
        "issues": [
            {"number": 1, "state": "OPEN", "title": "x",
             "labels": [], "depends_on_parsed": [],
             "fields": {"duration_days": {"value": 3}}},
        ]
    }, today=date(2026, 4, 15))
    fns[0](baseline, out)
    assert len(out.charts) == 1
    # Chart has a properly-scaled trace
    assert out.charts[0]["data"][0]["x"][0] == 3 * 86_400_000
    print("test_build_gantt_figure_reachable_from_exec_namespace: OK")


def test_synthesize_dates_closed_from_gh_timestamps() -> None:
    """Closed issues without project-board dates get start/end from
    createdAt / closedAt (trimmed to YYYY-MM-DD)."""
    data = {
        "issues": [
            {
                "number": 11,
                "state": "CLOSED",
                "createdAt": "2026-04-11T12:30:00Z",
                "closedAt": "2026-04-15T09:00:00Z",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {"duration_days": {"value": 4}},
            }
        ]
    }
    out = sc.synthesize_dates(data)
    i11 = out["issues"][0]
    assert sc.get_field(i11, "start_date") == "2026-04-11"
    assert sc.get_field(i11, "end_date") == "2026-04-15"
    # Source tagged as synthesized
    cells = i11["fields"]
    assert cells["start_date"]["source"] == "synthesized"
    print("test_synthesize_dates_closed_from_gh_timestamps: OK")


def test_synthesize_dates_open_forward_projects_from_today() -> None:
    """Open issues with no dates and no predecessors start today,
    end today + duration_days."""
    today = date(2026, 4, 15)
    data = {
        "issues": [
            {
                "number": 1,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {"duration_days": {"value": 5}},
            }
        ]
    }
    out = sc.synthesize_dates(data, today=today)
    i = out["issues"][0]
    assert sc.get_field(i, "start_date") == "2026-04-15"
    assert sc.get_field(i, "end_date") == "2026-04-20"
    print("test_synthesize_dates_open_forward_projects_from_today: OK")


def test_synthesize_dates_respects_dependency_chain() -> None:
    """Open issue that depends on another issue starts after that one
    finishes, not at 'today'."""
    today = date(2026, 4, 15)
    data = {
        "issues": [
            {
                "number": 1,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {"duration_days": {"value": 10}},
            },
            {
                "number": 2,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [1],
                "fields": {"duration_days": {"value": 3}},
            },
        ]
    }
    out = sc.synthesize_dates(data, today=today)
    by_num = {i["number"]: i for i in out["issues"]}
    # Issue 1: today → today + 10d
    assert sc.get_field(by_num[1], "end_date") == "2026-04-25"
    # Issue 2: starts after #1 ends, not "today"
    assert sc.get_field(by_num[2], "start_date") == "2026-04-25"
    assert sc.get_field(by_num[2], "end_date") == "2026-04-28"
    print("test_synthesize_dates_respects_dependency_chain: OK")


def test_synthesize_dates_preserves_existing_dates() -> None:
    """Issues that already have both start and end are not touched."""
    data = {
        "issues": [
            {
                "number": 1,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {
                    "start_date": {"value": "2026-05-01", "source": "github"},
                    "end_date": {"value": "2026-05-05", "source": "github"},
                    "duration_days": {"value": 4},
                },
            }
        ]
    }
    out = sc.synthesize_dates(data, today=date(2026, 4, 15))
    i = out["issues"][0]
    assert sc.get_field(i, "start_date") == "2026-05-01"
    assert i["fields"]["start_date"]["source"] == "github"  # unchanged
    print("test_synthesize_dates_preserves_existing_dates: OK")


def test_synthesize_dates_is_idempotent() -> None:
    """Running synthesis twice yields the same result as running once."""
    data = {
        "issues": [
            {
                "number": 1,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {"duration_days": {"value": 7}},
            }
        ]
    }
    once = sc.synthesize_dates(data, today=date(2026, 4, 15))
    twice = sc.synthesize_dates(once, today=date(2026, 4, 15))
    assert once["issues"][0]["fields"] == twice["issues"][0]["fields"]
    print("test_synthesize_dates_is_idempotent: OK")


def test_synthesize_dates_does_not_mutate_input() -> None:
    data = {
        "issues": [
            {
                "number": 1,
                "state": "OPEN",
                "labels": [],
                "depends_on_parsed": [],
                "fields": {"duration_days": {"value": 3}},
            }
        ]
    }
    snap = json.dumps(data, sort_keys=True)
    sc.synthesize_dates(data, today=date(2026, 4, 15))
    assert json.dumps(data, sort_keys=True) == snap
    print("test_synthesize_dates_does_not_mutate_input: OK")


def test_get_field_unwraps_envelope_and_handles_missing() -> None:
    """get_field() is the defensive alternative to raw field access.
    Tells the LLM-generated code how to survive issues that lack
    start_date / end_date (most open issues in a real project)."""
    issue = {
        "fields": {
            "duration_days": {"value": 5, "source": "heuristic"},
            "raw_field": 42,  # no envelope
        }
    }
    assert sc.get_field(issue, "duration_days") == 5
    assert sc.get_field(issue, "raw_field") == 42
    assert sc.get_field(issue, "missing") is None
    assert sc.get_field(issue, "missing", default="fallback") == "fallback"
    # Empty issue → None
    assert sc.get_field({}, "duration_days") is None
    assert sc.get_field({"fields": None}, "duration_days") is None
    print("test_get_field_unwraps_envelope_and_handles_missing: OK")


def test_get_field_reachable_from_exec_namespace() -> None:
    """Generated scenarios.py can call get_field directly without
    importing anything — it's pre-loaded in the exec namespace."""
    src = """
@scenario("uses get_field")
def s(data, out):
    total = 0
    for issue in data["issues"]:
        total += get_field(issue, "duration_days", default=0)
    out.metric("total", total)
    return data
"""
    fns = sc._exec_scenarios_source(src)
    assert len(fns) == 1
    out = sc.Out(name=fns[0]._scenario_name)
    baseline = {
        "issues": [
            {"fields": {"duration_days": {"value": 5}}},
            {"fields": {}},  # missing — must not crash
            {"fields": {"duration_days": {"value": 3}}},
        ]
    }
    fns[0](baseline, out)
    # 5 + 0 (default) + 3 = 8
    assert ("total", "8") in out.metrics
    print("test_get_field_reachable_from_exec_namespace: OK")


def test_exec_namespace_has_working_import() -> None:
    """The restricted exec namespace must support `import X` at runtime
    for whitelisted modules — fixed the `__import__ not found` bug that
    the AST validator alone couldn't catch because it only parses."""
    src = """
import copy
from datetime import timedelta

@scenario("uses stdlib")
def s(data, out):
    dup = copy.deepcopy(data)
    out.metric("count", len(dup["issues"]))
    return dup
"""
    fns = sc._exec_scenarios_source(src)
    assert len(fns) == 1
    # Actually invoke it — exec succeeded but the import machinery
    # still has to work INSIDE the function body.
    out = sc.Out(name=fns[0]._scenario_name)
    result = fns[0]({"issues": [{"number": 1}]}, out)
    assert result is not None
    assert ("count", "1") in out.metrics
    print("test_exec_namespace_has_working_import: OK")


def test_exec_namespace_rejects_unsafe_import_at_runtime() -> None:
    """Even if the AST validator somehow missed a forbidden import,
    the runtime `_safe_import` shim refuses to load it. Defence in
    depth — both layers must fail for a bad module to load."""
    # We can't easily write source that the AST accepts but whose import
    # isn't whitelisted, because AST screens imports. So we test
    # _safe_import directly.
    try:
        sc._safe_import("subprocess")
    except ImportError as exc:
        assert "not allowed" in str(exc)
        print("test_exec_namespace_rejects_unsafe_import_at_runtime: OK")
        return
    assert False, "expected ImportError for subprocess"


def test_exec_namespace_rejects_relative_import() -> None:
    """Relative imports (e.g. `from . import foo`) are refused — no
    legitimate use in a scenarios.py, and they open attack paths via
    package context fiddling."""
    try:
        sc._safe_import("anything", level=1)
    except ImportError as exc:
        assert "relative" in str(exc).lower()
        print("test_exec_namespace_rejects_relative_import: OK")
        return
    assert False, "expected ImportError for relative import"


async def test_validator_accepts_now_allowed_imports() -> None:
    """copy / itertools / functools / collections / math / json / typing / re /
    statistics — all pure stdlib the generator commonly reaches for. Adding
    them to the safe list (from only `datetime`) fixed the "sandbox
    restriction" fallback loop we hit in production."""
    for mod in ("copy", "itertools", "functools", "collections", "math", "json", "typing", "re", "statistics"):
        src = f"""
import {mod}

@scenario("ok")
def s(data, out):
    return data
"""
        sc._validate_scenarios_source(src)
    print("test_validator_accepts_now_allowed_imports: OK")


async def test_validator_allows_getattr_literal() -> None:
    """getattr() was previously forbidden outright — relaxed so that
    `getattr(obj, "name")` is fine. The dunder-access check still
    blocks `getattr(obj, "__class__")`-style attacks."""
    src = """
@scenario("ok")
def s(data, out):
    out.metric("x", getattr(data, "issue_count", 0))
    return data
"""
    sc._validate_scenarios_source(src)
    print("test_validator_allows_getattr_literal: OK")


async def test_generator_retries_on_validation_failure() -> None:
    """When the backend's first output fails validation, the retry loop
    feeds the error back and lets the backend try again. Second attempt
    returns valid source → session starts."""
    attempts: list[list[str]] = []

    async def flaky_gen(descriptions):
        attempts.append(list(descriptions))
        # First call returns invalid source (imports forbidden module).
        # Second call (with retry note in the description) returns valid.
        if len(attempts) == 1:
            return "import socket\n@scenario('x')\ndef s(d,o): return d"
        return """
@scenario("fixed")
def s1(data, out):
    return data

@scenario("also fixed")
def s2(data, out):
    return data
"""

    sc.set_generator_backend(flaky_gen)
    try:
        session_id, outs = await sc.start_session(["a", "b"], _baseline())
    finally:
        sc.set_generator_backend(None)

    assert session_id.startswith("scn-")
    assert len(outs) == 2
    assert len(attempts) == 2, f"expected 2 backend calls, got {len(attempts)}"
    # Retry note should have been appended to the last description
    assert "[RETRY 1]" in attempts[1][-1]
    assert "socket" in attempts[1][-1]  # the rejection reason
    print("test_generator_retries_on_validation_failure: OK")


async def test_generator_gives_up_after_max_retries() -> None:
    """If every retry still fails validation, raise ScenarioValidationError."""
    attempts: list[int] = []

    async def persistent_fail(descriptions):
        attempts.append(len(attempts) + 1)
        return "import os\n@scenario('x')\ndef s(d,o): return d"

    sc.set_generator_backend(persistent_fail)
    try:
        try:
            await sc.start_session(["a", "b"], _baseline())
        except sc.ScenarioValidationError as exc:
            # Should have hit 1 initial + MAX_GENERATOR_RETRIES retries = 3 total
            assert len(attempts) == 1 + sc.MAX_GENERATOR_RETRIES, len(attempts)
            assert "os" in str(exc)
            print("test_generator_gives_up_after_max_retries: OK")
            return
        finally:
            sc.set_generator_backend(None)
    except Exception:
        sc.set_generator_backend(None)
        raise
    assert False, "expected ScenarioValidationError after retries exhausted"


async def test_generator_output_validated_before_save() -> None:
    """If the generator returns forbidden Python, start_session refuses
    BEFORE writing any session files."""

    async def malicious_gen(descriptions):
        return "import socket\n@scenario('x')\ndef s(d,o): pass"

    sc.set_generator_backend(malicious_gen)
    try:
        try:
            await sc.start_session(["a", "b"], _baseline())
        except sc.ScenarioValidationError as exc:
            assert "socket" in str(exc)
            print("test_generator_output_validated_before_save: OK")
            return
        finally:
            sc.set_generator_backend(None)
    except Exception as exc:
        sc.set_generator_backend(None)
        raise
    assert False, "expected ScenarioValidationError"


# ---------- runner ----------


async def amain() -> None:
    sync_tests = [
        test_out_metric_list_text_serialize,
        test_out_chart_accepts_dict,
        test_out_chart_rejects_wrong_type,
        test_scenario_decorator_attaches_name,
        test_scenario_decorator_rejects_empty_name,
        test_delay_all_shifts_both_dates,
        test_delay_all_does_not_mutate_input,
        test_delay_issue_cascades_to_dependents,
        test_drop_issue_removes_and_prunes_deps,
        test_scale_durations_scales_and_recomputes_end,
        test_scale_durations_where_filter,
        test_scale_durations_rejects_non_positive,
        test_shift_start_anchors_to_new_date,
        test_exclude_weekends_stretches_end_dates,
        test_freeze_after_drops_later_issues,
        test_validator_accepts_safe_source,
        test_validator_rejects_os_import,
        test_validator_rejects_exec_call,
        test_validator_rejects_dunder_access,
        test_short_issue_label_recognises_various_phase_tags,
        test_build_gantt_figure_labels_use_short_format,
        test_build_gantt_figure_scales_to_milliseconds,
        test_build_gantt_figure_colours_by_state,
        test_build_gantt_figure_skips_undated_issues,
        test_build_gantt_figure_reachable_from_exec_namespace,
        test_synthesize_dates_closed_from_gh_timestamps,
        test_synthesize_dates_open_forward_projects_from_today,
        test_synthesize_dates_respects_dependency_chain,
        test_synthesize_dates_preserves_existing_dates,
        test_synthesize_dates_is_idempotent,
        test_synthesize_dates_does_not_mutate_input,
        test_get_field_unwraps_envelope_and_handles_missing,
        test_get_field_reachable_from_exec_namespace,
        test_exec_namespace_has_working_import,
        test_exec_namespace_rejects_unsafe_import_at_runtime,
        test_exec_namespace_rejects_relative_import,
    ]
    async_tests = [
        test_start_session_with_fake_generator,
        test_commit_and_close_session,
        test_commit_out_of_range_rejected,
        test_list_sessions_newest_first,
        test_apply_active_scenario_composes,
        test_apply_active_scenario_noop_when_no_commit,
        test_start_session_rejects_too_few_or_too_many,
        test_validator_accepts_now_allowed_imports,
        test_validator_allows_getattr_literal,
        test_generator_retries_on_validation_failure,
        test_generator_gives_up_after_max_retries,
        test_generator_output_validated_before_save,
    ]
    for t in sync_tests:
        t()
    for t in async_tests:
        await t()
    print(f"\nAll {len(sync_tests) + len(async_tests)} scenarios tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
