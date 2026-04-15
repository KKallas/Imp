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
    ]
    async_tests = [
        test_start_session_with_fake_generator,
        test_commit_and_close_session,
        test_commit_out_of_range_rejected,
        test_list_sessions_newest_first,
        test_apply_active_scenario_composes,
        test_apply_active_scenario_noop_when_no_commit,
        test_start_session_rejects_too_few_or_too_many,
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
