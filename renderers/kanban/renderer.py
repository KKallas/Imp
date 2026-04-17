"""renderers/kanban — Kanban board renderer.

Wraps ``pipeline.render_chart.build_context_for_kanban``.
"""

from __future__ import annotations

from typing import Any

from renderers.base import BaseRenderer


class KanbanRenderer(BaseRenderer):
    name = "kanban"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        from pipeline.render_chart import build_context_for_kanban, load_enriched

        if isinstance(raw, dict) and raw.get("issues"):
            enriched = raw
        else:
            enriched = load_enriched()
        return build_context_for_kanban(enriched)
