"""renderers/scatter — Scatter plot renderer.

Accepts x/y data arrays and renders an interactive Plotly scatter chart.
Data is passed via URL query parameters.
"""

from __future__ import annotations

import json
from typing import Any

from renderers.base import BaseRenderer


class ScatterRenderer(BaseRenderer):
    name = "scatter"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, str):
            raw = json.loads(raw)
        assert isinstance(raw, dict)
        x = raw.get("x", [])
        y = raw.get("y", [])
        labels = raw.get("labels", [])
        title = raw.get("title", "Scatter Plot")
        x_label = raw.get("x_label", "X")
        y_label = raw.get("y_label", "Y")

        figure = {
            "data": [
                {
                    "x": x,
                    "y": y,
                    "text": labels,
                    "type": "scatter",
                    "mode": "markers+text",
                    "textposition": "top center",
                    "marker": {"size": 10, "color": "#2563eb"},
                }
            ],
            "layout": {
                "title": {"text": title, "font": {"size": 16}},
                "xaxis": {"title": x_label},
                "yaxis": {"title": y_label},
                "template": "plotly_white",
                "margin": {"l": 50, "r": 30, "t": 60, "b": 50},
            },
        }
        return {"figure_json": json.dumps(figure)}
