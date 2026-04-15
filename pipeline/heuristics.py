#!/usr/bin/env python3
"""pipeline/heuristics.py — infer durations, dependencies, delays.

Reads `.imp/issues.json` (from `sync_issues.py`), enriches each issue
with inferred fields, and writes `.imp/enriched.json` for downstream
chart rendering and scenario analysis.

## What gets enriched

Each field in `issue.fields` is upgraded from a flat scalar to a dict
that carries its provenance:

  {
    "value": <whatever>,
    "source": "github" | "heuristic" | "llm",
    "confidence": "high" | "medium" | "low"
  }

Fields the sync already populated from gh keep `source: "github"` with
high confidence. Empty fields get heuristic defaults (low / medium
confidence) so downstream scripts always have a value to render.

Two new top-level keys per issue:

  - `depends_on_parsed` — `[12, 15]` or `[]`. Always present.
    Unparseable tokens go into `depends_on_unparseable` and are logged
    to stderr. Per v0.1.md, parse failures are soft — the issue is
    treated as having no known dependencies for that run, not aborted.
  - `delay` — only present when the issue is detected as delayed.
    Comparing current `end_date` against today + the `imp:baseline`
    label state (per the AC).

A top-level `dependency_edges` array on the enriched payload makes the
graph easy for `render_chart.py` to consume directly.

## Stale-data guard

The sync timestamp from `.imp/issues.json` is propagated unchanged to
`.imp/enriched.json` plus a fresh `enriched_at` timestamp, so callers
can detect stale enrichment vs. stale sync independently.

## Read-only

No GitHub side effects. Classified as a read by `server/intercept.py`
(see PIPELINE_READ_SCRIPTS) — Foreman's `run_heuristics` tool runs it
without burning the edits or tasks budget.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / ".imp" / "issues.json"
OUTPUT_FILE = ROOT / ".imp" / "enriched.json"

# Default duration in days when nothing else informs us.
DEFAULT_DURATION_DAYS = 3

# Coarse per-label hints — not authoritative, just better than the
# global default. Tuned from the existing P-numbering convention in
# this repo's issues; refine over time.
DURATION_HINT_BY_LABEL: dict[str, int] = {
    "area:server": 3,
    "area:pipeline": 2,
    "area:ui": 2,
    "imp:baseline": 5,
}


# ---------- depends_on parsing ----------

# Per v0.1.md: "split on commas, strip `#`, ignore anything that isn't
# an integer". Extra whitespace + leading `#` is fine; everything else
# is unparseable and gets logged.
_INT_TOKEN_RE = re.compile(r"^#?(\d+)$")


def parse_depends_on(text: str) -> tuple[list[int], list[str]]:
    """Parse a depends_on text field, return (parsed_issue_numbers, unparseable_tokens).

    Always succeeds — no exceptions. Unparseable tokens come back in
    the second tuple slot so the caller can log/surface them without
    aborting the enrichment run.
    """
    if not text or not isinstance(text, str):
        return ([], [])
    parsed: list[int] = []
    bad: list[str] = []
    for raw in re.split(r"[,\s]+", text.strip()):
        if not raw:
            continue
        m = _INT_TOKEN_RE.match(raw)
        if m:
            parsed.append(int(m.group(1)))
        else:
            bad.append(raw)
    return (parsed, bad)


# ---------- duration inference ----------


def _labels_of(issue: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                out.append(name)
        elif isinstance(label, str):
            out.append(label)
    return out


def infer_duration(issue: dict[str, Any]) -> tuple[int, str, str]:
    """Return (days, source, confidence) for an issue's duration.

    If the GitHub project field already has it, we use that with
    `source: github / confidence: high` — there's no inference.
    Otherwise we take the **largest** hint across matching labels and
    fall back to the global default. Largest-wins is intentional: for a
    PM tool, it's safer to overestimate than to underestimate, and it
    decouples the result from GitHub's label display order.
    """
    fields = issue.get("fields") or {}
    raw = fields.get("duration_days")
    if isinstance(raw, (int, float)) and raw > 0:
        return (int(raw), "github", "high")

    hints = [
        DURATION_HINT_BY_LABEL[label]
        for label in _labels_of(issue)
        if label in DURATION_HINT_BY_LABEL
    ]
    if hints:
        return (max(hints), "heuristic", "medium")

    return (DEFAULT_DURATION_DAYS, "heuristic", "low")


# ---------- delay detection ----------


def detect_delay(
    issue: dict[str, Any], today: date | None = None
) -> dict[str, Any] | None:
    """Return a delay record if the issue is overdue per its baseline.

    Per the AC: comparing current `end_date` to the `imp:baseline` label
    state. We interpret that as: an issue with the `imp:baseline` label
    that's still OPEN past its `end_date` is delayed. Issues without
    the label (newly added, scope creep, etc.) aren't subject to this
    check.

    Closed issues, even if past their date, aren't flagged — they shipped.
    """
    today = today or date.today()
    state = str(issue.get("state") or "").upper()
    if state != "OPEN":
        return None

    if "imp:baseline" not in _labels_of(issue):
        return None

    fields = issue.get("fields") or {}
    end_date_str = fields.get("end_date")
    if not isinstance(end_date_str, str):
        return None

    try:
        end = date.fromisoformat(end_date_str)
    except ValueError:
        return None  # malformed — soft skip

    if end >= today:
        return None  # not yet due

    return {
        "is_delayed": True,
        "days_overdue": (today - end).days,
        "baseline_end_date": end_date_str,
        "reason": (
            f"end_date {end_date_str} passed; issue still OPEN "
            f"(baseline label present)"
        ),
        "source": "heuristic",
        "confidence": "high",
    }


# ---------- enrichment ----------


def _provenance_existing(value: Any) -> dict[str, Any]:
    """Wrap a sync-supplied value in the standard provenance envelope."""
    return {"value": value, "source": "github", "confidence": "high"}


def enrich_issue(
    issue: dict[str, Any], today: date | None = None
) -> dict[str, Any]:
    """Return a new dict — does NOT mutate `issue`.

    `today` is overridable for testing the delay branch deterministically.
    """
    raw_fields = issue.get("fields") or {}
    enriched_fields: dict[str, Any] = {}

    # 1. Wrap every existing concrete field in provenance envelopes.
    for key, value in raw_fields.items():
        if value is None:
            continue
        enriched_fields[key] = _provenance_existing(value)

    # 2. Duration: prefer github value (already wrapped above); else heuristic.
    if "duration_days" not in enriched_fields:
        days, source, confidence = infer_duration(issue)
        enriched_fields["duration_days"] = {
            "value": days,
            "source": source,
            "confidence": confidence,
        }

    # 3. depends_on parsing — always run, even if value is empty/null.
    raw_depends = raw_fields.get("depends_on")
    parsed, bad = parse_depends_on(str(raw_depends or ""))
    if bad:
        # Log to stderr but DON'T abort the run.
        print(
            f"[heuristics] issue #{issue.get('number')}: unparseable "
            f"depends_on tokens: {bad!r}",
            file=sys.stderr,
        )
    # Replace the raw text envelope with a structured value.
    enriched_fields["depends_on"] = {
        "value": parsed,
        "raw": raw_depends,
        "unparseable": bad,
        "source": "heuristic",
        "confidence": "high" if not bad else "medium",
    }

    out = dict(issue)
    out["fields"] = enriched_fields
    out["depends_on_parsed"] = parsed
    if bad:
        out["depends_on_unparseable"] = bad

    delay = detect_delay(issue, today=today)
    if delay is not None:
        out["delay"] = delay

    return out


def build_dependency_edges(
    enriched_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Emit `[{from: 12, to: 5}, ...]` for downstream graph rendering.

    `from -> to` reads as "issue 12 depends on issue 5"; render_chart
    treats `to` as the predecessor. Edges to issues that aren't in the
    sync's issue set are still emitted (the chart can show them as
    external).
    """
    edges: list[dict[str, Any]] = []
    for issue in enriched_issues:
        src = issue.get("number")
        if not isinstance(src, int):
            continue
        for dep in issue.get("depends_on_parsed") or []:
            if isinstance(dep, int) and dep != src:
                edges.append({"from": src, "to": dep})
    return edges


def enrich(
    payload: dict[str, Any], today: date | None = None
) -> dict[str, Any]:
    """Take a sync_issues output dict, return the enriched dict."""
    issues = payload.get("issues") or []
    enriched_issues = [enrich_issue(it, today=today) for it in issues]
    return {
        **payload,
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "issues": enriched_issues,
        "dependency_edges": build_dependency_edges(enriched_issues),
        "delayed_count": sum(1 for it in enriched_issues if it.get("delay")),
    }


# ---------- I/O ----------


def load_input(path: Path = INPUT_FILE) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `pipeline/sync_issues.py` first"
        )
    return json.loads(path.read_text())


def write_output(payload: dict[str, Any], path: Path = OUTPUT_FILE) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=INPUT_FILE,
        help=f"Path to sync_issues output (default {INPUT_FILE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help=f"Path to enriched output (default {OUTPUT_FILE})",
    )
    args = parser.parse_args()

    try:
        payload = load_input(args.input)
    except Exception as exc:  # noqa: BLE001 — surface to Foreman
        print(str(exc), file=sys.stderr)
        return 1

    enriched = enrich(payload)
    write_output(enriched, args.output)

    print(
        f"Enriched {len(enriched['issues'])} issues "
        f"({enriched['delayed_count']} delayed, "
        f"{len(enriched['dependency_edges'])} dependency edges) "
        f"→ {args.output}",
        file=sys.stderr,
    )
    print(json.dumps(enriched, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
