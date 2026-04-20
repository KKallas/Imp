"""renderers/kanban — Kanban board renderer (self-contained)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from renderers.base import BaseRenderer
from renderers.helpers import field_value, load_enriched


def _normalize_status(raw: Any) -> str | None:
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


def build_context(enriched: dict[str, Any]) -> dict[str, Any]:
    issues = enriched.get("issues") or []
    columns: dict[str, dict] = {
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


class KanbanRenderer(BaseRenderer):
    name = "kanban"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("issues"):
            return build_context(raw)
        return build_context(load_enriched())
