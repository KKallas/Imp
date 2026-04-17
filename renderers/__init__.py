"""renderers — plugin discovery for the template-based rendering service.

Each subdirectory that contains a ``renderer.py`` is treated as a plugin.
``discover()`` imports them all and returns a ``{name: instance}`` map.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from renderers.base import BaseRenderer

_PLUGINS_DIR = Path(__file__).parent

_cache: dict[str, BaseRenderer] | None = None


def discover(*, force: bool = False) -> dict[str, BaseRenderer]:
    """Scan ``renderers/*/renderer.py`` and return one instance per plugin.

    Results are cached after the first call; pass ``force=True`` to
    re-scan (useful in tests).
    """
    global _cache  # noqa: PLW0603
    if _cache is not None and not force:
        return _cache

    from renderers.base import BaseRenderer as _Base

    found: dict[str, _Base] = {}
    for subdir in sorted(_PLUGINS_DIR.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith(("_", ".")):
            continue
        if not (subdir / "renderer.py").exists():
            continue
        module = importlib.import_module(f"renderers.{subdir.name}.renderer")
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, _Base)
                and obj is not _Base
            ):
                inst = obj()
                found[inst.name] = inst
    _cache = found
    return found


def get(name: str) -> BaseRenderer | None:
    """Shortcut: ``renderers.get("mermaid")``."""
    return discover().get(name)
