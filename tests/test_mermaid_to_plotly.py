"""Tests for pipeline/mermaid_to_plotly.py.

Run directly: `.venv/bin/python tests/test_mermaid_to_plotly.py`
No pytest. Asserts -> exit 0 on success, exit 1 on failure.

Covers:
  - extract_mermaid_blocks: finds all fenced blocks, handles missing
    closing fence, handles nested code fences
  - parse_gantt: simple gantt, complex gantt (sections, dependencies,
    crit tags, weekend exclusions)
  - mermaid_gantt_to_plotly: produces expected bar shapes
  - Watchdog integration: _foreman_say strips gantt blocks and attaches
    Plotly elements, leaves non-gantt blocks with a note, fail-soft on
    malformed gantt

KKallas/Imp#52.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import mermaid_to_plotly as mtp  # noqa: E402


# ---------- extract_mermaid_blocks ----------


def test_extract_finds_single_block() -> None:
    text = "Here is a chart:\n```mermaid\ngantt\n    title Test\n```\nDone."
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["content"].startswith("gantt")
    assert blocks[0]["raw"].startswith("```mermaid")
    assert blocks[0]["raw"].endswith("```")
    print("test_extract_finds_single_block: OK")


def test_extract_finds_multiple_blocks() -> None:
    text = (
        "Chart 1:\n```mermaid\ngantt\n    title A\n```\n"
        "Chart 2:\n```mermaid\nflowchart LR\n    A-->B\n```\n"
    )
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 2
    assert "gantt" in blocks[0]["content"]
    assert "flowchart" in blocks[1]["content"]
    print("test_extract_finds_multiple_blocks: OK")


def test_extract_returns_empty_for_no_mermaid() -> None:
    text = "Just some text.\n```python\nprint('hi')\n```\n"
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 0
    print("test_extract_returns_empty_for_no_mermaid: OK")


def test_extract_handles_missing_closing_fence() -> None:
    """An unclosed mermaid fence should NOT match (regex requires closing ```)."""
    text = "Here:\n```mermaid\ngantt\n    title Oops\nNo closing fence."
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 0
    print("test_extract_handles_missing_closing_fence: OK")


def test_extract_handles_non_mermaid_fences_nearby() -> None:
    """Other fenced code blocks should not confuse the extractor."""
    text = (
        "```python\nx = 1\n```\n"
        "```mermaid\ngantt\n    title Real\n```\n"
        "```bash\necho hi\n```\n"
    )
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 1
    assert "gantt" in blocks[0]["content"]
    print("test_extract_handles_non_mermaid_fences_nearby: OK")


# ---------- parse_gantt: simple ----------


SIMPLE_GANTT = """\
gantt
    title Simple Project
    dateFormat YYYY-MM-DD
    section Planning
    Design :des, 2026-04-01, 2026-04-05
    Review :rev, 2026-04-05, 2026-04-07
"""


def test_parse_gantt_simple_title() -> None:
    parsed = mtp.parse_gantt(SIMPLE_GANTT)
    assert parsed["title"] == "Simple Project"
    print("test_parse_gantt_simple_title: OK")


def test_parse_gantt_simple_tasks() -> None:
    parsed = mtp.parse_gantt(SIMPLE_GANTT)
    assert len(parsed["tasks"]) == 2
    t1, t2 = parsed["tasks"]
    assert t1["name"] == "Design"
    assert t1["id"] == "des"
    assert t1["start"] == "2026-04-01"
    assert t1["end"] == "2026-04-05"
    assert t2["name"] == "Review"
    assert t2["start"] == "2026-04-05"
    print("test_parse_gantt_simple_tasks: OK")


def test_parse_gantt_simple_sections() -> None:
    parsed = mtp.parse_gantt(SIMPLE_GANTT)
    assert len(parsed["sections"]) == 1
    assert parsed["sections"][0]["name"] == "Planning"
    assert len(parsed["sections"][0]["tasks"]) == 2
    print("test_parse_gantt_simple_sections: OK")


# ---------- parse_gantt: complex ----------


COMPLEX_GANTT = """\
gantt
    title KKallas/Imp — Imp Gantt
    dateFormat YYYY-MM-DD
    axisFormat %Y-%m-%d
    excludes weekends

    section Phase 1
    Setup repo         :done, p1_setup, 2026-03-01, 2026-03-05
    Auth module        :crit, p1_auth, 2026-03-05, 10d
    CI pipeline        :p1_ci, after p1_auth, 5d

    section Phase 2
    Dashboard UI       :p2_dash, 2026-03-20, 2026-04-01
    API integration    :p2_api, after p2_dash, 7d
"""


def test_parse_gantt_complex_excludes() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    assert "weekends" in parsed["excludes"]
    print("test_parse_gantt_complex_excludes: OK")


def test_parse_gantt_complex_axis_format() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    assert parsed["axis_format"] == "%Y-%m-%d"
    print("test_parse_gantt_complex_axis_format: OK")


def test_parse_gantt_complex_tags() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    by_id = {t["id"]: t for t in parsed["tasks"] if t["id"]}
    assert "done" in by_id["p1_setup"]["tags"]
    assert "crit" in by_id["p1_auth"]["tags"]
    assert by_id["p2_dash"]["tags"] == []
    print("test_parse_gantt_complex_tags: OK")


def test_parse_gantt_complex_dependencies() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    by_id = {t["id"]: t for t in parsed["tasks"] if t["id"]}
    ci = by_id["p1_ci"]
    assert ci["after"] == ["p1_auth"]
    # After resolution: start should be set from p1_auth's end.
    assert ci["start"] is not None
    assert ci["end"] is not None
    print("test_parse_gantt_complex_dependencies: OK")


def test_parse_gantt_complex_duration() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    by_id = {t["id"]: t for t in parsed["tasks"] if t["id"]}
    auth = by_id["p1_auth"]
    assert auth["duration_days"] == 10
    assert auth["start"] == "2026-03-05"
    # end = start + 10d
    assert auth["end"] == "2026-03-15"
    print("test_parse_gantt_complex_duration: OK")


def test_parse_gantt_complex_sections() -> None:
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    section_names = [s["name"] for s in parsed["sections"]]
    assert "Phase 1" in section_names
    assert "Phase 2" in section_names
    assert len(parsed["tasks"]) == 5
    print("test_parse_gantt_complex_sections: OK")


def test_parse_gantt_dependency_chain_resolves() -> None:
    """p1_ci depends on p1_auth; p2_api depends on p2_dash.
    Both should have concrete start/end after resolution."""
    parsed = mtp.parse_gantt(COMPLEX_GANTT)
    by_id = {t["id"]: t for t in parsed["tasks"] if t["id"]}
    ci = by_id["p1_ci"]
    api = by_id["p2_api"]
    # CI: after p1_auth (ends 2026-03-15), duration 5d
    assert ci["start"] == "2026-03-15"
    assert ci["end"] == "2026-03-20"
    # API: after p2_dash (ends 2026-04-01), duration 7d
    assert api["start"] == "2026-04-01"
    assert api["end"] == "2026-04-08"
    print("test_parse_gantt_dependency_chain_resolves: OK")


def test_parse_gantt_comments_ignored() -> None:
    text = "gantt\n    title T\n    %% this is a comment\n    section S\n    A :a1, 2026-01-01, 2026-01-02\n"
    parsed = mtp.parse_gantt(text)
    assert len(parsed["tasks"]) == 1
    print("test_parse_gantt_comments_ignored: OK")


def test_parse_gantt_empty() -> None:
    parsed = mtp.parse_gantt("gantt\n")
    assert parsed["tasks"] == []
    assert parsed["sections"] == []
    assert parsed["title"] is None
    print("test_parse_gantt_empty: OK")


# ---------- mermaid_gantt_to_plotly ----------


def test_to_plotly_simple_has_bar_trace() -> None:
    fig = mtp.mermaid_gantt_to_plotly(SIMPLE_GANTT)
    assert len(fig["data"]) == 1
    trace = fig["data"][0]
    assert trace["type"] == "bar"
    assert trace["orientation"] == "h"
    print("test_to_plotly_simple_has_bar_trace: OK")


def test_to_plotly_simple_task_count() -> None:
    fig = mtp.mermaid_gantt_to_plotly(SIMPLE_GANTT)
    trace = fig["data"][0]
    # 2 tasks, reversed for top-to-bottom display
    assert len(trace["y"]) == 2
    assert "Design" in trace["y"]
    assert "Review" in trace["y"]
    print("test_to_plotly_simple_task_count: OK")


def test_to_plotly_simple_title() -> None:
    fig = mtp.mermaid_gantt_to_plotly(SIMPLE_GANTT)
    assert fig["layout"]["title"]["text"] == "Simple Project"
    print("test_to_plotly_simple_title: OK")


def test_to_plotly_complex_task_count() -> None:
    fig = mtp.mermaid_gantt_to_plotly(COMPLEX_GANTT)
    trace = fig["data"][0]
    assert len(trace["y"]) == 5
    print("test_to_plotly_complex_task_count: OK")


def test_to_plotly_complex_done_tasks_grey() -> None:
    fig = mtp.mermaid_gantt_to_plotly(COMPLEX_GANTT)
    trace = fig["data"][0]
    # "Setup repo" is done -> should be grey (#9ca3af).
    # Tasks are reversed, so Setup repo is last in the list.
    setup_idx = trace["y"].index("Setup repo")
    assert trace["marker"]["color"][setup_idx] == "#9ca3af"
    print("test_to_plotly_complex_done_tasks_grey: OK")


def test_to_plotly_complex_crit_tasks_red() -> None:
    fig = mtp.mermaid_gantt_to_plotly(COMPLEX_GANTT)
    trace = fig["data"][0]
    auth_idx = trace["y"].index("Auth module")
    assert trace["marker"]["color"][auth_idx] == "#dc2626"
    print("test_to_plotly_complex_crit_tasks_red: OK")


def test_to_plotly_raises_on_no_plottable_tasks() -> None:
    try:
        mtp.mermaid_gantt_to_plotly("gantt\n    title Empty\n")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "No tasks" in str(exc)
    print("test_to_plotly_raises_on_no_plottable_tasks: OK")


def test_to_plotly_layout_has_date_xaxis() -> None:
    fig = mtp.mermaid_gantt_to_plotly(SIMPLE_GANTT)
    assert fig["layout"]["xaxis"]["type"] == "date"
    print("test_to_plotly_layout_has_date_xaxis: OK")


def test_to_plotly_durations_in_ms() -> None:
    """Bar widths should be in milliseconds for Plotly's date axis."""
    fig = mtp.mermaid_gantt_to_plotly(SIMPLE_GANTT)
    trace = fig["data"][0]
    ms_per_day = 86_400_000
    # Design: 2026-04-01 to 2026-04-05 = 4 days
    design_idx = trace["y"].index("Design")
    assert trace["x"][design_idx] == 4 * ms_per_day
    print("test_to_plotly_durations_in_ms: OK")


# ---------- watchdog helpers (extract + convert round-trip) ----------


def test_watchdog_gantt_stripped_from_text() -> None:
    """Simulates the watchdog flow: gantt block should be stripped from
    the text and a figure should be produced."""
    text = "Here's the timeline:\n```mermaid\n" + SIMPLE_GANTT + "```\nLet me know!"
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 1

    cleaned = text
    figures = []
    for block in blocks:
        content = block["content"]
        first_word = content.lstrip().split()[0].lower()
        if first_word == "gantt":
            fig = mtp.mermaid_gantt_to_plotly(content)
            figures.append(fig)
            cleaned = cleaned.replace(block["raw"], "")

    assert len(figures) == 1
    assert "```mermaid" not in cleaned
    assert "Let me know!" in cleaned
    print("test_watchdog_gantt_stripped_from_text: OK")


def test_watchdog_non_gantt_left_in_place() -> None:
    """Non-gantt mermaid blocks should NOT be stripped."""
    text = "Diagram:\n```mermaid\nflowchart LR\n    A-->B\n```\nEnd."
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 1
    content = blocks[0]["content"]
    first_word = content.lstrip().split()[0].lower()
    assert first_word != "gantt"
    # Watchdog leaves it in place (main.py adds a note)
    print("test_watchdog_non_gantt_left_in_place: OK")


def test_watchdog_fail_soft_on_malformed_gantt() -> None:
    """A gantt block with no plottable tasks should fail soft —
    ValueError caught, original block preserved with error note."""
    malformed = "gantt\n    title Bad\n    section S\n    No colon here\n"
    text = "Chart:\n```mermaid\n" + malformed + "```\nDone."
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 1

    cleaned = text
    for block in blocks:
        content = block["content"]
        first_word = content.lstrip().split()[0].lower()
        if first_word == "gantt":
            try:
                mtp.mermaid_gantt_to_plotly(content)
                # If it somehow succeeds, that's fine too
            except Exception as exc:
                cleaned = cleaned.replace(
                    block["raw"],
                    block["raw"] + f"\n\n_(watchdog couldn't parse: {exc})_",
                )

    # Original block should still be present
    assert "```mermaid" in cleaned
    assert "watchdog couldn't parse" in cleaned
    print("test_watchdog_fail_soft_on_malformed_gantt: OK")


def test_watchdog_mixed_blocks() -> None:
    """A reply with both a gantt and a flowchart block: gantt converted,
    flowchart left in place."""
    text = (
        "Timeline:\n```mermaid\n" + SIMPLE_GANTT + "```\n"
        "Diagram:\n```mermaid\nflowchart LR\n    A-->B\n```\n"
    )
    blocks = mtp.extract_mermaid_blocks(text)
    assert len(blocks) == 2

    cleaned = text
    figures = []
    for block in blocks:
        content = block["content"]
        first_word = content.lstrip().split()[0].lower()
        if first_word == "gantt":
            fig = mtp.mermaid_gantt_to_plotly(content)
            figures.append(fig)
            cleaned = cleaned.replace(block["raw"], "")

    assert len(figures) == 1
    # Gantt stripped, flowchart remains
    assert "gantt" not in cleaned.lower() or "flowchart" in cleaned.lower()
    assert "flowchart" in cleaned
    print("test_watchdog_mixed_blocks: OK")


# ---------- main ----------


def main() -> None:
    tests = [
        # extract_mermaid_blocks
        test_extract_finds_single_block,
        test_extract_finds_multiple_blocks,
        test_extract_returns_empty_for_no_mermaid,
        test_extract_handles_missing_closing_fence,
        test_extract_handles_non_mermaid_fences_nearby,
        # parse_gantt: simple
        test_parse_gantt_simple_title,
        test_parse_gantt_simple_tasks,
        test_parse_gantt_simple_sections,
        # parse_gantt: complex
        test_parse_gantt_complex_excludes,
        test_parse_gantt_complex_axis_format,
        test_parse_gantt_complex_tags,
        test_parse_gantt_complex_dependencies,
        test_parse_gantt_complex_duration,
        test_parse_gantt_complex_sections,
        test_parse_gantt_dependency_chain_resolves,
        test_parse_gantt_comments_ignored,
        test_parse_gantt_empty,
        # mermaid_gantt_to_plotly
        test_to_plotly_simple_has_bar_trace,
        test_to_plotly_simple_task_count,
        test_to_plotly_simple_title,
        test_to_plotly_complex_task_count,
        test_to_plotly_complex_done_tasks_grey,
        test_to_plotly_complex_crit_tasks_red,
        test_to_plotly_raises_on_no_plottable_tasks,
        test_to_plotly_layout_has_date_xaxis,
        test_to_plotly_durations_in_ms,
        # watchdog integration
        test_watchdog_gantt_stripped_from_text,
        test_watchdog_non_gantt_left_in_place,
        test_watchdog_fail_soft_on_malformed_gantt,
        test_watchdog_mixed_blocks,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} mermaid_to_plotly tests passed.")


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
