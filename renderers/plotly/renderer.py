"""renderers/plotly — Plotly figure renderer.

Accepts a Plotly figure dict (``{"data": [...], "layout": {...}}``)
and renders it via ``plotly.js`` in a standalone page.
"""

from __future__ import annotations

import json
from typing import Any

from renderers.base import BaseRenderer


class PlotlyRenderer(BaseRenderer):
    name = "plotly"
    block_type = None  # not triggered by markdown — used via API

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, str):
            figure = json.loads(raw)
        else:
            figure = raw
        return {"figure_json": json.dumps(figure)}
