#!/usr/bin/env python3
"""pipeline/render_chart.py — render a chart from .imp/enriched.json.

Reads heuristics output, builds a chart-specific context, renders the
matching Jinja2 template, and writes self-contained HTML to
`.imp/output/<template>.html`.

P4.14 ships the **gantt** template only; kanban / burndown / comparison
land in P4.16 (scenario.py) and P4.19 (extra templates) — but the
plumbing handles them as soon as their `.j2` files exist.

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


# Per-template context builders. Add new entries as kanban /
# burndown / comparison templates land in later phases.
CONTEXT_BUILDERS: dict[str, Any] = {
    "gantt": build_context_for_gantt,
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

    context = builder(enriched)
    html = render_html(args.template, context)
    out = write_html(html, args.template, args.output_dir)

    print(
        f"Rendered {args.template} chart with {len(context.get('renderable_issues') or [])} "
        f"issues, {len(context.get('missing_issues') or [])} missing dates "
        f"→ {out}",
        file=sys.stderr,
    )
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
