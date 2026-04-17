"""renderers/mermaid — Generic Mermaid diagram renderer.

Accepts raw Mermaid syntax (any diagram type — flowchart, sequence,
gantt, etc.) and renders it via ``mermaid.min.js`` in a standalone page.
Triggered automatically when assistant output contains a fenced
````mermaid`` code block.
"""

from __future__ import annotations

from typing import Any

from renderers.base import BaseRenderer


class MermaidRenderer(BaseRenderer):
    name = "mermaid"
    block_type = "mermaid"

    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict):
            diagram = raw.get("diagram", "")
        else:
            diagram = raw
        return {"diagram": str(diagram).strip()}
