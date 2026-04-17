"""renderers/comparison — Side-by-side scenario comparison renderer.

Wraps ``pipeline.render_chart.build_context_for_comparison``.
"""

from __future__ import annotations

from typing import Any

from renderers.base import BaseRenderer


class ComparisonRenderer(BaseRenderer):
    name = "comparison"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        from pipeline.render_chart import (
            build_context_for_comparison,
            load_enriched,
        )

        if isinstance(raw, dict) and raw.get("issues"):
            enriched = raw
        else:
            enriched = load_enriched()
        return build_context_for_comparison(enriched)
