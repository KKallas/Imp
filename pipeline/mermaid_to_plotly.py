"""pipeline/mermaid_to_plotly.py — parse mermaid gantt blocks into Plotly figures.

The Foreman agent occasionally emits mermaid gantt syntax in chat replies
even though Chainlit can't render mermaid natively. This module provides a
line-based parser that converts those blocks into Plotly horizontal-bar
figures that Chainlit *can* render inline via `cl.Plotly`.

No external dependencies — the mermaid gantt grammar is small enough to
handle directly with string splitting.

KKallas/Imp#52.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any


# ---------- mermaid block extraction ----------


_MERMAID_FENCE_RE = re.compile(
    r"```mermaid\s*\n(.*?)```",
    re.DOTALL,
)


def extract_mermaid_blocks(text: str) -> list[dict[str, str]]:
    """Find all fenced mermaid code blocks in *text*.

    Returns a list of ``{"raw": "```mermaid\\n...```", "content": "..."}``
    dicts. ``raw`` is the full fenced block (for replacement in the
    source text); ``content`` is the inner text without the fences.
    """
    blocks: list[dict[str, str]] = []
    for m in _MERMAID_FENCE_RE.finditer(text):
        blocks.append({"raw": m.group(0), "content": m.group(1).strip()})
    return blocks


# ---------- gantt parser ----------

# Supported directives (case-insensitive first word):
#   gantt
#   title <text>
#   dateFormat <fmt>
#   axisFormat <fmt>
#   excludes weekends
#   excludes <date>
#   section <name>
#   <task line>

# Task-line grammar (mermaid is surprisingly lenient):
#   Task name :tag, id, after dep1 dep2, 3d
#   Task name :tag, id, 2026-04-01, 3d
#   Task name :id, 2026-04-01, 2026-04-05
#   Task name :id, after dep, 2026-04-05
# Tags: done, active, crit, milestone (we only care about done/crit for
# visual mapping).

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DURATION_RE = re.compile(r"(\d+)([dwhm])")
_AFTER_RE = re.compile(r"after\s+([\w\s]+)")


def _parse_duration(token: str) -> timedelta | None:
    """Parse a mermaid duration token like ``3d``, ``1w``, ``2h``."""
    m = _DURATION_RE.fullmatch(token.strip())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(days=n * 30)
    return None


def _parse_date(token: str) -> date | None:
    """Parse a YYYY-MM-DD date, returning None on failure."""
    m = _DATE_RE.fullmatch(token.strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(0))
    except ValueError:
        return None


_TAGS = {"done", "active", "crit", "milestone"}


def parse_gantt(text: str) -> dict[str, Any]:
    """Parse a mermaid gantt block into a structured dict.

    Returns::

        {
            "title": str | None,
            "date_format": str,
            "axis_format": str | None,
            "excludes": list[str],
            "sections": [{"name": str, "tasks": [...]}],
            "tasks": [<all tasks flat>],
        }

    Each task::

        {
            "name": str,
            "id": str | None,
            "start": "YYYY-MM-DD" | None,
            "end": "YYYY-MM-DD" | None,
            "duration_days": int | None,
            "after": list[str],  # dependency IDs
            "tags": list[str],   # done, crit, active, milestone
            "section": str,
        }
    """
    title: str | None = None
    date_format = "YYYY-MM-DD"
    axis_format: str | None = None
    excludes: list[str] = []
    sections: list[dict[str, Any]] = []
    current_section = "Default"
    all_tasks: list[dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("%%"):
            continue  # blank or comment

        low = line.lower()

        # Directives
        if low == "gantt":
            continue
        if low.startswith("title "):
            title = line[6:].strip()
            continue
        if low.startswith("dateformat "):
            date_format = line[11:].strip()
            continue
        if low.startswith("axisformat "):
            axis_format = line[11:].strip()
            continue
        if low.startswith("excludes "):
            excludes.append(line[9:].strip())
            continue
        if low.startswith("section "):
            current_section = line[8:].strip()
            continue
        if low.startswith("todaymarker "):
            continue  # recognised but unused

        # Everything else is a task line: "Task name :tokens..."
        task = _parse_task_line(line, current_section)
        if task:
            # Make sure the section exists in the list.
            sec = next((s for s in sections if s["name"] == current_section), None)
            if sec is None:
                sec = {"name": current_section, "tasks": []}
                sections.append(sec)
            sec["tasks"].append(task)
            all_tasks.append(task)

    # Resolve `after` dependencies — compute start/end dates from
    # predecessors so every task has concrete dates for plotting.
    _resolve_dependencies(all_tasks)

    return {
        "title": title,
        "date_format": date_format,
        "axis_format": axis_format,
        "excludes": excludes,
        "sections": sections,
        "tasks": all_tasks,
    }


def _parse_task_line(line: str, section: str) -> dict[str, Any] | None:
    """Parse a single mermaid gantt task line.

    Expected form: ``Task name :spec1, spec2, ...``
    The colon separates the display name from the comma-separated specs.
    """
    if ":" not in line:
        return None
    name_part, _, spec_part = line.partition(":")
    name = name_part.strip()
    if not name:
        return None

    tokens = [t.strip() for t in spec_part.split(",") if t.strip()]

    task_id: str | None = None
    start: date | None = None
    end: date | None = None
    duration: timedelta | None = None
    after: list[str] = []
    tags: list[str] = []

    for token in tokens:
        low = token.lower()

        # Tag?
        if low in _TAGS:
            tags.append(low)
            continue

        # Duration?
        dur = _parse_duration(token)
        if dur is not None:
            duration = dur
            continue

        # Date?
        dt = _parse_date(token)
        if dt is not None:
            if start is None:
                start = dt
            else:
                end = dt
            continue

        # After dependency?
        after_m = _AFTER_RE.fullmatch(token)
        if after_m:
            after.extend(after_m.group(1).split())
            continue

        # Otherwise treat as a task ID (mermaid allows bare identifiers).
        if re.fullmatch(r"[\w-]+", token):
            task_id = token
            continue

    # Derive end from start + duration if we got both.
    if start and duration and not end:
        end = start + duration
    # If only end and duration, derive start.
    if end and duration and not start:
        start = end - duration

    return {
        "name": name,
        "id": task_id,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "duration_days": duration.days if duration else (
            (end - start).days if start and end else None
        ),
        "after": after,
        "tags": tags,
        "section": section,
    }


def _resolve_dependencies(tasks: list[dict[str, Any]]) -> None:
    """Fill in start/end dates for tasks that use ``after`` dependencies.

    Walks the task list (which is in declaration order) and resolves
    ``after`` references by looking up predecessor end dates. This is a
    single-pass approach — mermaid requires dependencies to be declared
    before the tasks that reference them.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for t in tasks:
        if t["id"]:
            by_id[t["id"]] = t

    for t in tasks:
        if not t["after"]:
            continue

        # Find the latest end date among dependencies.
        latest_end: date | None = None
        for dep_id in t["after"]:
            dep = by_id.get(dep_id)
            if dep and dep.get("end"):
                dep_end = date.fromisoformat(dep["end"])
                if latest_end is None or dep_end > latest_end:
                    latest_end = dep_end

        if latest_end is None:
            continue

        # Set start to the day after the latest dependency ends.
        if t["start"] is None:
            t["start"] = latest_end.isoformat()

        # If we have duration but no end, compute it.
        if t["end"] is None and t["duration_days"] is not None:
            start_dt = date.fromisoformat(t["start"])
            t["end"] = (start_dt + timedelta(days=t["duration_days"])).isoformat()


# ---------- plotly figure builder ----------


# Colour palette — section-based so tasks in the same section share a colour.
_SECTION_COLORS = [
    "#2563eb",  # blue
    "#16a34a",  # green
    "#dc2626",  # red
    "#9333ea",  # purple
    "#ea580c",  # orange
    "#0891b2",  # teal
    "#c026d3",  # pink
    "#ca8a04",  # yellow
]

_DONE_COLOR = "#9ca3af"  # grey for completed tasks
_CRIT_COLOR = "#dc2626"  # red for critical/delayed tasks


def mermaid_gantt_to_plotly(text: str) -> dict[str, Any]:
    """Parse a mermaid gantt block and return a Plotly figure dict.

    The figure uses horizontal bars (one per task) grouped by section,
    matching the visual style of `build_burndown_plotly_figure` in
    render_chart.py.

    Raises ``ValueError`` if the text contains no plottable tasks (no
    tasks with both a start and end date).
    """
    parsed = parse_gantt(text)
    return gantt_to_plotly_figure(parsed)


def gantt_to_plotly_figure(parsed: dict[str, Any]) -> dict[str, Any]:
    """Convert a parsed gantt dict into a Plotly figure dict.

    Raises ``ValueError`` when there are no plottable tasks.
    """
    tasks = parsed.get("tasks") or []
    plottable = [t for t in tasks if t.get("start") and t.get("end")]
    if not plottable:
        raise ValueError("No tasks with start and end dates to plot")

    # Build section → colour mapping.
    sections_seen: list[str] = []
    for t in plottable:
        sec = t.get("section") or "Default"
        if sec not in sections_seen:
            sections_seen.append(sec)
    section_color = {
        sec: _SECTION_COLORS[i % len(_SECTION_COLORS)]
        for i, sec in enumerate(sections_seen)
    }

    # Build bar traces — one bar per task, bottom-to-top so the first
    # task declared appears at the top of the chart.
    task_names: list[str] = []
    starts: list[str] = []
    durations: list[int] = []
    colors: list[str] = []

    for t in reversed(plottable):
        label = t["name"]
        # Disambiguate tasks with the same name by appending section.
        if label in task_names:
            label = f"{label} ({t.get('section', '')})"
        task_names.append(label)
        starts.append(t["start"])

        start_dt = date.fromisoformat(t["start"])
        end_dt = date.fromisoformat(t["end"])
        dur = max(1, (end_dt - start_dt).days)
        durations.append(dur)

        # Colour: done → grey, crit → red, else section colour.
        tags = t.get("tags") or []
        if "done" in tags:
            colors.append(_DONE_COLOR)
        elif "crit" in tags:
            colors.append(_CRIT_COLOR)
        else:
            colors.append(section_color.get(t.get("section", "Default"), _SECTION_COLORS[0]))

    # Build the Plotly figure as a dict (no plotly import needed —
    # Chainlit accepts raw JSON dicts).
    # Use a single bar trace with per-bar colours via marker.color.
    # x = duration in milliseconds (Plotly timeline needs base + width
    # in the same unit). Simpler approach: use a horizontal bar chart
    # with x = [start, end] pairs rendered via base + width.
    #
    # Actually, the cleanest Plotly approach for gantt is to use
    # one shape per task. But for cl.Plotly we need a figure dict.
    # We'll use a bar chart with base= start dates and x = durations.

    # Convert durations to milliseconds for the date axis.
    ms_per_day = 86_400_000
    widths_ms = [d * ms_per_day for d in durations]

    figure: dict[str, Any] = {
        "data": [
            {
                "type": "bar",
                "orientation": "h",
                "y": task_names,
                "x": widths_ms,
                "base": starts,
                "marker": {"color": colors},
                "hovertemplate": (
                    "%{y}<br>"
                    "Start: %{base}<br>"
                    "Duration: %{customdata} days"
                    "<extra></extra>"
                ),
                "customdata": durations,
            }
        ],
        "layout": {
            "title": {
                "text": parsed.get("title") or "Gantt",
                "font": {"size": 15},
            },
            "xaxis": {
                "type": "date",
                "title": "",
            },
            "yaxis": {
                "automargin": True,
            },
            "bargap": 0.3,
            "template": "plotly_white",
            "margin": {"l": 20, "r": 20, "t": 50, "b": 30},
            "height": max(300, len(task_names) * 35 + 100),
        },
    }

    return figure
