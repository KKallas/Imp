"""renderers/bar — Bar graph renderer.

Accepts labels and values arrays and renders an interactive Plotly bar chart.
Data is passed via URL query parameters.
"""

from __future__ import annotations

import json
from typing import Any

from renderers.base import BaseRenderer


class BarRenderer(BaseRenderer):
    name = "bar"
    block_type = None

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, str):
            raw = json.loads(raw)
        assert isinstance(raw, dict)
        labels = raw.get("labels", [])
        values = raw.get("values", [])
        title = raw.get("title", "Bar Chart")
        x_label = raw.get("x_label", "")
        y_label = raw.get("y_label", "Value")
        colors = raw.get("colors", None)

        figure = {
            "data": [
                {
                    "x": labels,
                    "y": values,
                    "type": "bar",
                    "marker": {
                        "color": colors
                        or [
                            "#2563eb",
                            "#16a34a",
                            "#dc2626",
                            "#9333ea",
                            "#ea580c",
                            "#0891b2",
                            "#c026d3",
                            "#ca8a04",
                        ][: len(labels)]
                    },
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
