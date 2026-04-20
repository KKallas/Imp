"""renderers/helpers.py — shared helpers for chart renderers.

Extracted from pipeline/render_chart.py. The CLI entrypoint is here too:
    python -m renderers.helpers --template gantt
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / ".imp" / "enriched.json"
OUTPUT_DIR = ROOT / ".imp" / "output"
RENDERERS_DIR = ROOT / "renderers"


# ---------- field unwrapping ----------


def field_value(issue: dict[str, Any], key: str) -> Any:
    """Return the .value of a wrapped field, or None if absent / null."""
    fields = issue.get("fields") or {}
    cell = fields.get(key)
    if isinstance(cell, dict) and "value" in cell:
        return cell["value"]
    return cell


# ---------- date math ----------


@dataclass
class IssueDates:
    renderable: bool
    start: str | None = None
    end: str | None = None
    derived_start: bool = False
    derived_end: bool = False
    why_unrenderable: str | None = None


def resolve_dates(issue: dict[str, Any]) -> IssueDates:
    """Pick the best (start, end) pair for a Gantt task line."""
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
            return IssueDates(renderable=True, start=start, end=d.isoformat(), derived_end=True)
        except ValueError:
            return IssueDates(renderable=False, why_unrenderable=f"bad start_date {start!r}")

    if end and duration is not None:
        try:
            d = date.fromisoformat(end) - timedelta(days=duration)
            return IssueDates(renderable=True, start=d.isoformat(), end=end, derived_start=True)
        except ValueError:
            return IssueDates(renderable=False, why_unrenderable=f"bad end_date {end!r}")

    return IssueDates(renderable=False, why_unrenderable="no start/end/duration combination")


def iso_date_from_raw(raw: Any) -> date | None:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


# ---------- I/O ----------


def load_enriched(path: Path = INPUT_FILE) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run pipeline/heuristics.py first")
    return json.loads(path.read_text())


def apply_active_scenario_safe(enriched: dict[str, Any]) -> dict[str, Any]:
    """Apply committed scenario transform, or pass through."""
    import sys as _sys
    try:
        # Use already-imported module if available (for test monkey-patching)
        if "scenarios" in _sys.modules:
            scenarios = _sys.modules["scenarios"]
        elif "pipeline.scenarios" in _sys.modules:
            scenarios = _sys.modules["pipeline.scenarios"]
        else:
            try:
                from pipeline import scenarios  # type: ignore
            except ImportError:
                import scenarios  # type: ignore[no-redef]
    except ImportError:
        return enriched
    try:
        active = scenarios.active_session()
    except Exception:
        return enriched
    if not active:
        return enriched
    try:
        return scenarios.apply_active_scenario(enriched)
    except Exception as exc:
        print(f"[render] apply_active_scenario failed: {exc}", file=sys.stderr)
        return enriched


# ---------- template rendering ----------


def jinja_env(template_name: str) -> Environment:
    template_dir = RENDERERS_DIR / template_name
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(template_name: str, context: dict[str, Any]) -> str:
    env = jinja_env(template_name)
    template = env.get_template("template.html.j2")
    return template.render(**context)


def write_html(html: str, template_name: str, output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{template_name}.html"
    path.write_text(html)
    return path


# ---------- CLI ----------


def main() -> int:
    # Import builders from renderers (lazy to avoid circular)
    from renderers.gantt.renderer import build_context
    from renderers.kanban.renderer import build_context as kanban_context
    from renderers.burndown.renderer import build_context as burndown_context
    from renderers.comparison.renderer import build_context as comparison_context

    BUILDERS = {
        "gantt": build_context,
        "kanban": kanban_context,
        "burndown": burndown_context,
        "comparison": comparison_context,
    }

    parser = argparse.ArgumentParser(description="Render a chart from enriched.json")
    parser.add_argument("--template", default="gantt")
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--input-b", type=Path, default=None)
    args = parser.parse_args()

    builder = BUILDERS.get(args.template)
    if builder is None:
        print(f"unknown template {args.template!r}; available: {', '.join(sorted(BUILDERS))}", file=sys.stderr)
        return 1

    template_path = RENDERERS_DIR / args.template / "template.html.j2"
    if not template_path.exists():
        print(f"template file {template_path} not found", file=sys.stderr)
        return 1

    try:
        enriched = load_enriched(args.input)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    enriched = apply_active_scenario_safe(enriched)

    if args.template == "comparison":
        variant = None
        if args.input_b is not None:
            try:
                variant = load_enriched(args.input_b)
            except Exception as exc:
                print(str(exc), file=sys.stderr)
                return 1
        context = builder(enriched, variant)
    else:
        context = builder(enriched)

    html = render_html(args.template, context)
    out = write_html(html, args.template, args.output_dir)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
