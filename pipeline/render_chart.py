#!/usr/bin/env python3
"""pipeline/render_chart.py — render a chart from .imp/enriched.json.

Reads heuristics output, builds a chart-specific context, renders the
matching Jinja2 template, and writes self-contained HTML to
`.imp/output/<template>.html`.

P4.14 ships the **gantt** template. P4.19 adds **kanban**, **burndown**,
and **comparison** — all consuming the same enriched.json schema.
Comparison additionally accepts `--input-b` for the variant payload;
when omitted, both sides show the same baseline.

## Charting choice — Mermaid

Per v0.1.md §The Agent's Role: "When you produce a chart, emit a
mermaid fenced code block in your response." We use the same Mermaid
rendering inside the standalone HTML so the chart looks the same in
the browser as it does in the chat. The HTML loads `mermaid.min.js`
from a CDN; everything else (CSS, structure) is inline so the file
is self-contained per the AC.

## Inputs / outputs

  - Input  : `.imp/enriched.json` (P4.13 heuristics output)
  - Output : `.imp/output/<template>.html`
  - Template dir: `templates/<template>.html.j2`

## Read-only

No GitHub side effects. Classified as a read by `server/intercept.py`
(see `PIPELINE_READ_SCRIPTS`). Foreman's `run_render_chart` tool calls
this without burning the edits or tasks budget.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / ".imp" / "enriched.json"
OUTPUT_DIR = ROOT / ".imp" / "output"
TEMPLATES_DIR = ROOT / "templates"


# ---------- field unwrapping ----------
#
# heuristics.py wraps every field value in a provenance envelope:
#   {"value": ..., "source": "...", "confidence": "..."}
# We don't care about provenance here — we just want the value.


def field_value(issue: dict[str, Any], key: str) -> Any:
    """Return the .value of a wrapped field, or None if absent / null."""
    fields = issue.get("fields") or {}
    cell = fields.get(key)
    if isinstance(cell, dict) and "value" in cell:
        return cell["value"]
    # Defensive — pass through if heuristics ever returns a flat value.
    return cell


# ---------- date math ----------


@dataclass
class IssueDates:
    """Result of resolving an issue's date triple to chart-renderable form.

    `start` and `end` are always populated when `renderable=True`; one
    or both may have been derived from duration_days. `derived_*` flags
    let templates flag inferences in the UI.
    """

    renderable: bool
    start: str | None = None
    end: str | None = None
    derived_start: bool = False
    derived_end: bool = False
    why_unrenderable: str | None = None


def resolve_dates(issue: dict[str, Any]) -> IssueDates:
    """Pick the best (start, end) pair for a Gantt task line.

    Priority:
      1. start + end  → use both, no derivation
      2. start + duration → end = start + duration
      3. end + duration → start = end - duration
      4. otherwise → unrenderable; goes to the "missing dates" list
    """
    start = field_value(issue, "start_date")
    end = field_value(issue, "end_date")
    duration_raw = field_value(issue, "duration_days")

    duration = None
    if isinstance(duration_raw, (int, float)) and duration_raw > 0:
        duration = int(duration_raw)

    if start and end:
        return IssueDates(renderable=True, start=start, end=end)

    if start and duration is not None:
        try:
            d = date.fromisoformat(start) + timedelta(days=duration)
            return IssueDates(
                renderable=True, start=start, end=d.isoformat(), derived_end=True
            )
        except ValueError:
            return IssueDates(
                renderable=False, why_unrenderable=f"bad start_date {start!r}"
            )

    if end and duration is not None:
        try:
            d = date.fromisoformat(end) - timedelta(days=duration)
            return IssueDates(
                renderable=True, start=d.isoformat(), end=end, derived_start=True
            )
        except ValueError:
            return IssueDates(
                renderable=False, why_unrenderable=f"bad end_date {end!r}"
            )

    return IssueDates(
        renderable=False, why_unrenderable="no start/end/duration combination"
    )


# ---------- mermaid syntax ----------

# Mermaid task IDs must match `[A-Za-z0-9_]+`. Use `i<number>` per issue.
_TASK_ID_RE = re.compile(r"^i\d+$")


def _task_id(number: int) -> str:
    return f"i{int(number)}"


def _section_for(issue: dict[str, Any]) -> str:
    """Group key for the gantt sections.

    Prefer milestone title; fall back to the first `area:*` label; else
    "Unscheduled". Trim section names to keep the chart readable.
    """
    milestone = issue.get("milestone")
    if isinstance(milestone, dict):
        title = milestone.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:80]

    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.startswith("area:"):
            return name

    return "Unscheduled"


def _sanitize_task_name(title: str) -> str:
    """Mermaid task names break on `:` and a few other characters.
    Replace them with safe substitutes so the gantt parses cleanly."""
    return (
        title.replace(":", " —")  # `[P4.11]:` → `[P4.11] —`
        .replace("#", "")
        .strip()[:80]
    )


def _resolved_dependencies(
    issue: dict[str, Any], renderable_numbers: set[int]
) -> list[int]:
    """Pick the depends_on entries that are also being charted —
    cross-section `after` clauses are fine in mermaid, but we can't
    point at issues that aren't in the gantt at all."""
    raw = issue.get("depends_on_parsed") or []
    return [n for n in raw if isinstance(n, int) and n in renderable_numbers]


def build_mermaid_gantt(
    enriched: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (mermaid_text, renderable_issues_meta, missing_meta).

    `renderable_issues_meta` is a list of `{number, title, start, end,
    derived_start, derived_end, section, dependencies}` for the template
    to optionally render alongside the chart.
    `missing_meta` lists issues that couldn't be drawn, with the reason.
    """
    issues = enriched.get("issues") or []

    renderable: list[tuple[dict[str, Any], IssueDates]] = []
    missing: list[dict[str, Any]] = []
    for issue in issues:
        dates = resolve_dates(issue)
        if dates.renderable:
            renderable.append((issue, dates))
        else:
            missing.append(
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "state": issue.get("state"),
                    "reason": dates.why_unrenderable,
                }
            )

    renderable_numbers = {
        int(i.get("number"))
        for i, _ in renderable
        if isinstance(i.get("number"), int)
    }

    # Group renderables by section, preserving order within each section.
    sections: dict[str, list[tuple[dict[str, Any], IssueDates]]] = {}
    for entry in renderable:
        sections.setdefault(_section_for(entry[0]), []).append(entry)

    title = enriched.get("repo") or "Project"
    lines: list[str] = [
        "gantt",
        f"    title {title} — Imp Gantt",
        "    dateFormat YYYY-MM-DD",
        "    axisFormat %Y-%m-%d",
    ]

    # Mermaid sorts sections in declaration order; sort alphabetically
    # so the rendering is deterministic across runs.
    renderable_meta: list[dict[str, Any]] = []
    for section_name in sorted(sections):
        lines.append(f"    section {section_name}")
        for issue, dates in sections[section_name]:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            tid = _task_id(number)
            name = _sanitize_task_name(str(issue.get("title") or f"Issue #{number}"))
            deps = _resolved_dependencies(issue, renderable_numbers)
            tags: list[str] = [tid]
            if str(issue.get("state") or "").upper() == "CLOSED":
                tags.insert(0, "done")
            elif issue.get("delay"):
                tags.insert(0, "crit")
            tag_clause = ", ".join(tags)
            if deps:
                after_clause = "after " + " ".join(_task_id(d) for d in deps)
                # We only need a duration when chaining via `after`, since
                # mermaid computes the start from the predecessors.
                start_dt = date.fromisoformat(dates.start)
                end_dt = date.fromisoformat(dates.end)
                # Mermaid demands a positive integer-day duration for `after`.
                duration_days = max(1, (end_dt - start_dt).days)
                lines.append(
                    f"    {name} :{tag_clause}, {after_clause}, {duration_days}d"
                )
            else:
                lines.append(
                    f"    {name} :{tag_clause}, {dates.start}, {dates.end}"
                )

            renderable_meta.append(
                {
                    "number": number,
                    "title": issue.get("title"),
                    "state": issue.get("state"),
                    "section": section_name,
                    "start": dates.start,
                    "end": dates.end,
                    "derived_start": dates.derived_start,
                    "derived_end": dates.derived_end,
                    "dependencies": deps,
                    "delayed": bool(issue.get("delay")),
                }
            )

    return ("\n".join(lines), renderable_meta, missing)


# ---------- template rendering ----------


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(
    template_name: str, context: dict[str, Any]
) -> str:
    env = _jinja_env()
    template = env.get_template(f"{template_name}.html.j2")
    return template.render(**context)


def build_context_for_gantt(enriched: dict[str, Any]) -> dict[str, Any]:
    mermaid, renderable, missing = build_mermaid_gantt(enriched)
    return {
        "title": enriched.get("repo", "Project"),
        "synced_at": enriched.get("synced_at"),
        "enriched_at": enriched.get("enriched_at"),
        "issue_count": enriched.get("issue_count", len(enriched.get("issues") or [])),
        "delayed_count": enriched.get("delayed_count", 0),
        "mermaid_text": mermaid,
        "renderable_issues": renderable,
        "missing_issues": missing,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- kanban ----------
#
# Status triage rules (first match wins):
#   1. fields.status — project-board status string ("Todo", "In Progress",
#      "Done", or close variants). Normalized by `_normalize_status`.
#   2. state == CLOSED → Done
#   3. state == OPEN + assignees present → In Progress
#   4. otherwise → Open
#
# `_KANBAN_COLUMNS` defines the display order and is also the contract
# the template renders against — (slug, label, matcher) triples.


def _normalize_status(raw: Any) -> str | None:
    """Map a project-board status string to a kanban column slug."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    if s in {"done", "closed", "completed", "complete"}:
        return "done"
    if s in {"in progress", "in-progress", "doing", "active", "wip"}:
        return "in-progress"
    if s in {"todo", "to do", "open", "backlog", "triage", "ready"}:
        return "open"
    return None


def _kanban_status(issue: dict[str, Any]) -> str:
    field = _normalize_status(field_value(issue, "status"))
    if field:
        return field
    if str(issue.get("state") or "").upper() == "CLOSED":
        return "done"
    if issue.get("assignees"):
        return "in-progress"
    return "open"


def _assignee_names(issue: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for a in issue.get("assignees") or []:
        if isinstance(a, dict):
            name = a.get("login") or a.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def build_context_for_kanban(enriched: dict[str, Any]) -> dict[str, Any]:
    issues = enriched.get("issues") or []
    columns: dict[str, dict[str, Any]] = {
        "open": {"slug": "open", "label": "Open", "cards": []},
        "in-progress": {"slug": "in-progress", "label": "In Progress", "cards": []},
        "done": {"slug": "done", "label": "Done", "cards": []},
    }
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        card = {
            "number": number,
            "title": str(issue.get("title") or f"Issue #{number}"),
            "assignees": _assignee_names(issue),
            "delayed": bool(issue.get("delay")),
        }
        columns[_kanban_status(issue)]["cards"].append(card)

    return {
        "title": enriched.get("repo", "Project"),
        "synced_at": enriched.get("synced_at"),
        "enriched_at": enriched.get("enriched_at"),
        "issue_count": enriched.get("issue_count", len(issues)),
        "columns": [columns["open"], columns["in-progress"], columns["done"]],
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- burndown ----------
#
# Burndown chart semantics (updated P4.19 follow-up):
#
# A burndown measures how much real work remains open on each day. We
# anchor on GH timestamps because they represent actual lifecycle
# events (issue filed, issue closed) — project-board start/end dates
# are *planned* dates and aren't what burndown is measuring.
#
# Per-issue resolution:
#   start    = createdAt (date portion); fallback to fields.start_date
#              only for fixtures / synthetic data without timestamps.
#   resolved = closedAt; fallback to fields.end_date, then updatedAt;
#              None for still-open issues (so they keep counting
#              toward "remaining" across the whole span).
#
# Exclusions: closed issues with stateReason == NOT_PLANNED are
# out-scoped, not completed. They don't enter scope and don't count
# as "burned down" — they're tallied separately as `excluded_count`
# so the reader can see *why* the numbers might differ from
# `gh issue list --state closed` counts.
#
# For each day in the span, remaining = count of tracked issues where
# start <= d AND (resolved is None OR resolved > d). Scope can grow
# mid-project (new issues filed), so the line is NOT strictly
# monotonic; the reference "ideal" line descends linearly from day-0
# scope to 0 over the span, as a visual benchmark only.


def _iso_date_from_raw(raw: Any) -> date | None:
    """Parse `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ` (gh timestamp) into
    a `date`. Returns None for unparseable / non-string input."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@dataclass
class _BurndownIssue:
    number: int | None
    title: str | None
    start: date
    resolved: date | None  # None = still open at end of span


# GH stateReason values that indicate the issue was closed *without*
# being completed — we treat these as out-of-scope work for burndown.
# Compared case-insensitively against issue["stateReason"].
_OUT_OF_SCOPE_STATE_REASONS: frozenset[str] = frozenset({"NOT_PLANNED"})


def _resolve_burndown_issue(
    issue: dict[str, Any],
) -> tuple[_BurndownIssue | None, str | None, str | None]:
    """Classify an enriched issue for burndown plotting.

    Returns `(tracked, excluded_reason, missing_reason)` — exactly one
    non-None. Excluded issues are tallied but omitted from scope and
    the missing list. Missing issues had no parseable creation date.
    """
    state = str(issue.get("state") or "").upper()
    state_reason = str(issue.get("stateReason") or "").upper()
    if state == "CLOSED" and state_reason in _OUT_OF_SCOPE_STATE_REASONS:
        return (None, f"closed as {state_reason}", None)

    start = _iso_date_from_raw(issue.get("createdAt"))
    if start is None:
        start = _iso_date_from_raw(field_value(issue, "start_date"))
    if start is None:
        return (None, None, "no createdAt or start_date")

    resolved: date | None = None
    if state == "CLOSED":
        resolved = (
            _iso_date_from_raw(issue.get("closedAt"))
            or _iso_date_from_raw(field_value(issue, "end_date"))
            or _iso_date_from_raw(issue.get("updatedAt"))
        )
        if resolved is None:
            # Closed but no timestamp anywhere — collapse to start so
            # it contributes to scope but resolves immediately. Rare.
            resolved = start
        elif resolved < start:
            resolved = start

    number = issue.get("number") if isinstance(issue.get("number"), int) else None
    return (
        _BurndownIssue(
            number=number,
            title=issue.get("title"),
            start=start,
            resolved=resolved,
        ),
        None,
        None,
    )


def _burndown_series(
    enriched: dict[str, Any], *, today: date | None = None
) -> tuple[
    list[str], list[int], list[float], int, int, int, int, list[dict[str, Any]]
]:
    """Return (labels, remaining, ideal, tracked, open_today, span_days,
    excluded, missing).

    `labels[i]` is an ISO date per day of the span (inclusive);
    `remaining[i]` is the count of tracked issues open at end of day
    `labels[i]`; `ideal[i]` is the straight-line reference burndown.
    `excluded` counts NOT_PLANNED closures (not plotted). `missing`
    lists issues with no usable creation timestamp.
    """
    issues = enriched.get("issues") or []
    tracked: list[_BurndownIssue] = []
    missing: list[dict[str, Any]] = []
    excluded = 0

    for issue in issues:
        meta, excluded_reason, missing_reason = _resolve_burndown_issue(issue)
        if excluded_reason:
            excluded += 1
            continue
        if missing_reason:
            missing.append(
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "reason": missing_reason,
                }
            )
            continue
        assert meta is not None
        tracked.append(meta)

    if not tracked:
        return ([], [], [], 0, 0, 0, excluded, missing)

    today = today or datetime.now(timezone.utc).date()
    project_start = min(t.start for t in tracked)
    resolved_dates = [t.resolved for t in tracked if t.resolved is not None]
    last_resolved = max(resolved_dates) if resolved_dates else project_start
    project_end = max(project_start, last_resolved, today)
    span_days = (project_end - project_start).days + 1

    labels: list[str] = []
    remaining: list[int] = []
    open_today = 0

    for offset in range(span_days):
        d = project_start + timedelta(days=offset)
        labels.append(d.isoformat())
        count = sum(
            1
            for t in tracked
            if t.start <= d and (t.resolved is None or t.resolved > d)
        )
        remaining.append(count)
        if d == today:
            open_today = count

    # If today falls before the span started (no issues yet),
    # open_today stays 0 by construction. The common case (today
    # inside or past the span) is handled by the loop above.

    # Ideal line: straight descent from initial in-scope count to 0.
    initial_scope = remaining[0] if remaining else 0
    if span_days > 1 and initial_scope > 0:
        step = initial_scope / (span_days - 1)
        ideal = [
            round(max(0.0, initial_scope - step * i), 2) for i in range(span_days)
        ]
    else:
        ideal = [float(initial_scope)] * span_days

    return (
        labels,
        remaining,
        ideal,
        len(tracked),
        open_today,
        span_days,
        excluded,
        missing,
    )


def build_context_for_burndown(enriched: dict[str, Any]) -> dict[str, Any]:
    (
        labels,
        remaining,
        ideal,
        tracked,
        open_today,
        span_days,
        excluded,
        missing,
    ) = _burndown_series(enriched)
    return {
        "title": enriched.get("repo", "Project"),
        "synced_at": enriched.get("synced_at"),
        "enriched_at": enriched.get("enriched_at"),
        "labels": labels,
        "remaining": remaining,
        "ideal": ideal,
        "tracked_count": tracked,
        "open_today": open_today,
        "span_days": span_days,
        "excluded_count": excluded,
        "missing_issues": missing,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- comparison ----------
#
# Renders TWO enriched payloads side-by-side: a mermaid gantt for each
# plus a per-issue delta table (variant end_date − baseline end_date,
# in days). Issues present in only one side are flagged via `only_in`.


def _gantt_end_by_number(
    mermaid_meta: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    return {
        int(m["number"]): m
        for m in mermaid_meta
        if isinstance(m.get("number"), int)
    }


def _delta_days(baseline_end: str | None, variant_end: str | None) -> int | None:
    if not baseline_end or not variant_end:
        return None
    try:
        b = date.fromisoformat(baseline_end)
        v = date.fromisoformat(variant_end)
    except ValueError:
        return None
    return (v - b).days


def build_context_for_comparison(
    baseline: dict[str, Any],
    variant: dict[str, Any] | None = None,
    *,
    baseline_label: str = "Baseline",
    variant_label: str = "Variant",
) -> dict[str, Any]:
    """Build a side-by-side comparison context. Falls back to baseline-only
    when `variant` is None (deltas are all 0 in that case)."""
    if variant is None:
        variant = baseline

    b_mermaid, b_meta, _ = build_mermaid_gantt(baseline)
    v_mermaid, v_meta, _ = build_mermaid_gantt(variant)

    b_by_num = _gantt_end_by_number(b_meta)
    v_by_num = _gantt_end_by_number(v_meta)

    all_numbers = sorted(set(b_by_num) | set(v_by_num))
    deltas: list[dict[str, Any]] = []
    for n in all_numbers:
        b = b_by_num.get(n)
        v = v_by_num.get(n)
        only_in: str | None = None
        if b and not v:
            only_in = "baseline"
        elif v and not b:
            only_in = "variant"
        title = (v or b or {}).get("title") or f"Issue #{n}"
        deltas.append(
            {
                "number": n,
                "title": title,
                "baseline_end": (b or {}).get("end"),
                "variant_end": (v or {}).get("end"),
                "delta_days": _delta_days(
                    (b or {}).get("end"), (v or {}).get("end")
                ),
                "only_in": only_in,
            }
        )

    title = baseline.get("repo") or variant.get("repo") or "Project"
    return {
        "title": title,
        "baseline_label": baseline_label,
        "variant_label": variant_label,
        "baseline_mermaid": b_mermaid if b_meta else "",
        "variant_mermaid": v_mermaid if v_meta else "",
        "baseline_count": baseline.get(
            "issue_count", len(baseline.get("issues") or [])
        ),
        "variant_count": variant.get(
            "issue_count", len(variant.get("issues") or [])
        ),
        "baseline_renderable": len(b_meta),
        "variant_renderable": len(v_meta),
        "deltas": deltas,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


# Per-template context builders. Comparison is wrapped to adapt
# the two-argument signature to the single-payload CLI surface;
# `main()` detects the comparison template and supplies --input-b
# separately.
CONTEXT_BUILDERS: dict[str, Any] = {
    "gantt": build_context_for_gantt,
    "kanban": build_context_for_kanban,
    "burndown": build_context_for_burndown,
    "comparison": build_context_for_comparison,
}


# ---------- I/O ----------


def load_enriched(path: Path = INPUT_FILE) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `pipeline/heuristics.py` first"
        )
    return json.loads(path.read_text())


def write_html(html: str, template_name: str, output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{template_name}.html"
    path.write_text(html)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        default="gantt",
        help="Template name under templates/ (default: gantt)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=INPUT_FILE,
        help=f"Path to enriched.json (default {INPUT_FILE})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Directory for the rendered HTML (default {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--input-b",
        type=Path,
        default=None,
        help=(
            "Second enriched.json for --template comparison (variant side). "
            "If omitted, both panels show the baseline."
        ),
    )
    args = parser.parse_args()

    builder = CONTEXT_BUILDERS.get(args.template)
    if builder is None:
        available = ", ".join(sorted(CONTEXT_BUILDERS)) or "(none)"
        print(
            f"unknown template {args.template!r}; available: {available}",
            file=sys.stderr,
        )
        return 1

    template_path = TEMPLATES_DIR / f"{args.template}.html.j2"
    if not template_path.exists():
        print(
            f"template file {template_path} not found", file=sys.stderr
        )
        return 1

    try:
        enriched = load_enriched(args.input)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1

    if args.template == "comparison":
        variant = None
        if args.input_b is not None:
            try:
                variant = load_enriched(args.input_b)
            except Exception as exc:  # noqa: BLE001
                print(str(exc), file=sys.stderr)
                return 1
        context = build_context_for_comparison(enriched, variant)
    else:
        context = builder(enriched)

    html = render_html(args.template, context)
    out = write_html(html, args.template, args.output_dir)

    # Per-template summary line — gantt reports renderable/missing;
    # burndown reports tracked/excluded/missing; kanban reports per-
    # column counts; comparison reports delta count.
    if args.template == "gantt":
        print(
            f"Rendered gantt chart with "
            f"{len(context.get('renderable_issues') or [])} issues, "
            f"{len(context.get('missing_issues') or [])} missing dates "
            f"→ {out}",
            file=sys.stderr,
        )
    elif args.template == "burndown":
        print(
            f"Rendered burndown: {context.get('tracked_count', 0)} tracked, "
            f"{context.get('excluded_count', 0)} out-scoped, "
            f"{len(context.get('missing_issues') or [])} missing → {out}",
            file=sys.stderr,
        )
    elif args.template == "kanban":
        counts = ", ".join(
            f"{c['label']}={len(c['cards'])}" for c in context.get("columns", [])
        )
        print(
            f"Rendered kanban: {counts} → {out}",
            file=sys.stderr,
        )
    elif args.template == "comparison":
        print(
            f"Rendered comparison: {len(context.get('deltas') or [])} issues "
            f"→ {out}",
            file=sys.stderr,
        )
    else:
        print(f"Rendered {args.template} → {out}", file=sys.stderr)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
