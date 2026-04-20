"""renderers/comparison — Side-by-side scenario comparison (self-contained)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from renderers.base import BaseRenderer
from renderers.helpers import load_enriched
from renderers.gantt.renderer import build_mermaid_gantt


def _gantt_end_by_number(mermaid_meta: list[dict]) -> dict[int, dict]:
    return {int(m["number"]): m for m in mermaid_meta if isinstance(m.get("number"), int)}


def _delta_days(baseline_end: str | None, variant_end: str | None) -> int | None:
    if not baseline_end or not variant_end:
        return None
    try:
        b = date.fromisoformat(baseline_end)
        v = date.fromisoformat(variant_end)
    except ValueError:
        return None
    return (v - b).days


def build_context(
    baseline: dict[str, Any],
    variant: dict[str, Any] | None = None,
    *,
    baseline_label: str = "Baseline",
    variant_label: str = "Variant",
) -> dict[str, Any]:
    if variant is None:
        variant = baseline

    b_mermaid, b_meta, _ = build_mermaid_gantt(baseline)
    v_mermaid, v_meta, _ = build_mermaid_gantt(variant)

    b_by_num = _gantt_end_by_number(b_meta)
    v_by_num = _gantt_end_by_number(v_meta)

    all_numbers = sorted(set(b_by_num) | set(v_by_num))
    deltas: list[dict] = []
    for n in all_numbers:
        b = b_by_num.get(n)
        v = v_by_num.get(n)
        only_in: str | None = None
        if b and not v:
            only_in = "baseline"
        elif v and not b:
            only_in = "variant"
        title = (v or b or {}).get("title") or f"Issue #{n}"
        deltas.append({
            "number": n, "title": title,
            "baseline_end": (b or {}).get("end"),
            "variant_end": (v or {}).get("end"),
            "delta_days": _delta_days((b or {}).get("end"), (v or {}).get("end")),
            "only_in": only_in,
        })

    title = baseline.get("repo") or variant.get("repo") or "Project"
    return {
        "title": title, "baseline_label": baseline_label, "variant_label": variant_label,
        "baseline_mermaid": b_mermaid if b_meta else "",
        "variant_mermaid": v_mermaid if v_meta else "",
        "baseline_count": baseline.get("issue_count", len(baseline.get("issues") or [])),
        "variant_count": variant.get("issue_count", len(variant.get("issues") or [])),
        "baseline_renderable": len(b_meta), "variant_renderable": len(v_meta),
        "deltas": deltas,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }


class ComparisonRenderer(BaseRenderer):
    name = "comparison"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict) and raw.get("issues"):
            return build_context(raw)
        return build_context(load_enriched())
