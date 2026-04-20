"""renderers/gantt — Gantt timeline renderer (self-contained)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from renderers.base import BaseRenderer
from renderers.helpers import field_value, resolve_dates, load_enriched, IssueDates


_TASK_ID_RE = re.compile(r"^i\d+$")


def _task_id(number: int) -> str:
    return f"i{int(number)}"


def _section_for(issue: dict[str, Any]) -> str:
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
    return title.replace(":", " —").replace("#", "").strip()[:80]


def _resolved_dependencies(issue: dict[str, Any], renderable_numbers: set[int]) -> list[int]:
    raw = issue.get("depends_on_parsed") or []
    return [n for n in raw if isinstance(n, int) and n in renderable_numbers]


def build_mermaid_gantt(enriched: dict[str, Any]) -> tuple[str, list[dict], list[dict]]:
    """Return (mermaid_text, renderable_meta, missing_meta)."""
    issues = enriched.get("issues") or []
    renderable: list[tuple[dict, IssueDates]] = []
    missing: list[dict] = []
    for issue in issues:
        dates = resolve_dates(issue)
        if dates.renderable:
            renderable.append((issue, dates))
        else:
            missing.append({
                "number": issue.get("number"), "title": issue.get("title"),
                "state": issue.get("state"), "reason": dates.why_unrenderable,
            })

    renderable_numbers = {int(i.get("number")) for i, _ in renderable if isinstance(i.get("number"), int)}
    sections: dict[str, list] = {}
    for entry in renderable:
        sections.setdefault(_section_for(entry[0]), []).append(entry)

    title = enriched.get("repo") or "Project"
    lines = ["gantt", f"    title {title} — Imp Gantt", "    dateFormat YYYY-MM-DD", "    axisFormat %Y-%m-%d"]
    renderable_meta: list[dict] = []

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
                start_dt = date.fromisoformat(dates.start)
                end_dt = date.fromisoformat(dates.end)
                duration_days = max(1, (end_dt - start_dt).days)
                lines.append(f"    {name} :{tag_clause}, {after_clause}, {duration_days}d")
            else:
                lines.append(f"    {name} :{tag_clause}, {dates.start}, {dates.end}")

            renderable_meta.append({
                "number": number, "title": issue.get("title"), "state": issue.get("state"),
                "section": section_name, "start": dates.start, "end": dates.end,
                "derived_start": dates.derived_start, "derived_end": dates.derived_end,
                "dependencies": deps, "delayed": bool(issue.get("delay")),
            })

    return ("\n".join(lines), renderable_meta, missing)


def build_context(enriched: dict[str, Any]) -> dict[str, Any]:
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


class GanttRenderer(BaseRenderer):
    name = "gantt"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("issues"):
            return build_context(raw)
        return build_context(load_enriched())
