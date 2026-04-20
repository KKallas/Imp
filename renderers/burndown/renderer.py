"""renderers/burndown — Burndown chart renderer (self-contained)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from renderers.base import BaseRenderer
from renderers.helpers import field_value, iso_date_from_raw, load_enriched


@dataclass
class _BurndownIssue:
    number: int | None
    title: str | None
    start: date
    resolved: date | None


_OUT_OF_SCOPE_STATE_REASONS: frozenset[str] = frozenset({"NOT_PLANNED"})


def _resolve_burndown_issue(issue: dict[str, Any]) -> tuple[_BurndownIssue | None, str | None, str | None]:
    state = str(issue.get("state") or "").upper()
    state_reason = str(issue.get("stateReason") or "").upper()
    if state == "CLOSED" and state_reason in _OUT_OF_SCOPE_STATE_REASONS:
        return (None, f"closed as {state_reason}", None)

    start = iso_date_from_raw(issue.get("createdAt"))
    if start is None:
        start = iso_date_from_raw(field_value(issue, "start_date"))
    if start is None:
        return (None, None, "no createdAt or start_date")

    resolved: date | None = None
    if state == "CLOSED":
        resolved = (
            iso_date_from_raw(issue.get("closedAt"))
            or iso_date_from_raw(field_value(issue, "end_date"))
            or iso_date_from_raw(issue.get("updatedAt"))
        )
        if resolved is None:
            resolved = start
        elif resolved < start:
            resolved = start

    number = issue.get("number") if isinstance(issue.get("number"), int) else None
    return (_BurndownIssue(number=number, title=issue.get("title"), start=start, resolved=resolved), None, None)


def _burndown_series(enriched: dict[str, Any], *, today: date | None = None):
    issues = enriched.get("issues") or []
    tracked: list[_BurndownIssue] = []
    missing: list[dict] = []
    excluded = 0

    for issue in issues:
        meta, excluded_reason, missing_reason = _resolve_burndown_issue(issue)
        if excluded_reason:
            excluded += 1
            continue
        if missing_reason:
            missing.append({"number": issue.get("number"), "title": issue.get("title"), "reason": missing_reason})
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
        count = sum(1 for t in tracked if t.start <= d and (t.resolved is None or t.resolved > d))
        remaining.append(count)
        if d == today:
            open_today = count

    initial_scope = remaining[0] if remaining else 0
    if span_days > 1 and initial_scope > 0:
        step = initial_scope / (span_days - 1)
        ideal = [round(max(0.0, initial_scope - step * i), 2) for i in range(span_days)]
    else:
        ideal = [float(initial_scope)] * span_days

    return (labels, remaining, ideal, len(tracked), open_today, span_days, excluded, missing)


def build_context(enriched: dict[str, Any]) -> dict[str, Any]:
    labels, remaining, ideal, tracked, open_today, span_days, excluded, missing = _burndown_series(enriched)
    return {
        "title": enriched.get("repo", "Project"),
        "synced_at": enriched.get("synced_at"),
        "enriched_at": enriched.get("enriched_at"),
        "labels": labels, "remaining": remaining, "ideal": ideal,
        "tracked_count": tracked, "open_today": open_today,
        "span_days": span_days, "excluded_count": excluded,
        "missing_issues": missing,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


def build_burndown_plotly_figure(context: dict[str, Any]) -> dict[str, Any] | None:
    labels = context.get("labels") or []
    if not labels:
        return None
    remaining = context.get("remaining") or []
    ideal = context.get("ideal") or []
    excluded = context.get("excluded_count") or 0
    title = context.get("title") or "Project"
    title_suffix = f" — {excluded} out-scoped excluded" if excluded else ""
    return {
        "data": [
            {"x": labels, "y": remaining, "type": "scatter", "mode": "lines+markers",
             "name": "Remaining (actual)", "line": {"color": "#2563eb", "width": 3}, "marker": {"size": 7}},
            {"x": labels, "y": ideal, "type": "scatter", "mode": "lines",
             "name": "Ideal", "line": {"color": "#9ca3af", "width": 1, "dash": "dash"}},
        ],
        "layout": {
            "title": {"text": f"Burndown — {title}{title_suffix}", "font": {"size": 15}},
            "xaxis": {"title": "Date", "type": "date"},
            "yaxis": {"title": "Open issues", "rangemode": "tozero", "dtick": 1},
            "legend": {"orientation": "h", "y": -0.2},
            "template": "plotly_white", "margin": {"l": 40, "r": 20, "t": 60, "b": 60},
        },
    }


class BurndownRenderer(BaseRenderer):
    name = "burndown"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("issues"):
            return build_context(raw)
        return build_context(load_enriched())
