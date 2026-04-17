"""renderers/burndown — Burndown chart renderer.

Wraps ``pipeline.render_chart.build_context_for_burndown``.
"""

from __future__ import annotations

from typing import Any

from renderers.base import BaseRenderer


class BurndownRenderer(BaseRenderer):
    name = "burndown"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        from pipeline.render_chart import build_context_for_burndown, load_enriched

        if isinstance(raw, dict) and raw.get("issues"):
            enriched = raw
        else:
            enriched = load_enriched()
        return build_context_for_burndown(enriched)
