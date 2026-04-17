"""renderers/gantt — Gantt timeline renderer.

Wraps ``pipeline.render_chart.build_context_for_gantt`` so the existing
builder logic is reused without duplication.  The enriched payload is
loaded from ``.imp/enriched.json`` when no data is provided via URL.
"""

from __future__ import annotations

from typing import Any

from renderers.base import BaseRenderer


class GanttRenderer(BaseRenderer):
    name = "gantt"
    block_type = None  # not triggered by markdown blocks

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        from pipeline.render_chart import build_context_for_gantt, load_enriched

        if isinstance(raw, dict) and raw.get("issues"):
            enriched = raw
        else:
            enriched = load_enriched()
        return build_context_for_gantt(enriched)
