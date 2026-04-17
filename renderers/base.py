"""renderers/base.py — BaseRenderer ABC for the plugin system.

Every renderer plugin lives in its own folder under ``renderers/`` and
exposes a class that extends ``BaseRenderer``.  Plugin discovery
(``renderers/__init__.py``) auto-scans subdirectories.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


class BaseRenderer(ABC):
    """Contract every renderer plugin must satisfy."""

    name: str  # folder name — used in /render/<name>
    block_type: str | None = None  # fenced-code lang tag; None = not triggered by markdown

    @abstractmethod
    def parse(self, raw: str | dict[str, Any]) -> dict[str, Any]:
        """Turn raw input (URL params dict or markdown block string)
        into a template-context dict ready for Jinja2 rendering."""

    def template_path(self) -> Path:
        """Absolute path to this plugin's Jinja2 template.

        Default: ``template.html.j2`` next to ``renderer.py``.
        """
        # __class__ resolves to the *concrete* subclass, so the path
        # points into the correct plugin folder.
        return Path(__file__).parent / self.name / "template.html.j2"

    def build_url(
        self, params: dict[str, Any], base: str = ""
    ) -> tuple[str, str]:
        """Return ``(image_url, viewer_url)`` for chat embedding.

        *image_url* returns a PNG screenshot (default mode).
        *viewer_url* returns the interactive HTML page (``mode=viewer``).
        """
        qs = urlencode(params, doseq=True)
        image_url = f"{base}/render/{self.name}?{qs}"
        viewer_url = f"{base}/render/{self.name}?{qs}&mode=viewer"
        return image_url, viewer_url
