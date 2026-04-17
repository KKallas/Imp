"""server/screenshot.py — HTML → PNG screenshot engine.

Uses ``html2image`` which leverages the system Chrome/Chromium already
installed on the machine — no separate browser download required.

Screenshots are cached by SHA-256 content hash so identical HTML always
returns the cached PNG without a browser round-trip.

Dependencies
------------
``pip install html2image``
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _ROOT / ".imp" / "output" / "screenshots"


def available() -> bool:
    """Return True when html2image is importable."""
    try:
        import html2image  # noqa: F401
        return True
    except ImportError:
        return False


# ── cache helpers ───────────────────────────────────────────────────

def _cache_key(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()


def _cached(key: str) -> bytes | None:
    path = _CACHE_DIR / f"{key}.png"
    if path.exists():
        return path.read_bytes()
    return None


def _store(key: str, png: bytes) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.png"
    path.write_bytes(png)
    return path


# ── public API ──────────────────────────────────────────────────────

async def screenshot(
    html: str,
    *,
    width: int = 1200,
    height: int = 800,
) -> bytes:
    """Render *html* in headless Chrome and return PNG bytes.

    Results are cached by content hash — identical HTML always
    returns the cached image without a browser round-trip.
    """
    key = _cache_key(html)
    hit = _cached(key)
    if hit is not None:
        return hit

    from html2image import Html2Image

    with tempfile.TemporaryDirectory() as tmpdir:
        hti = Html2Image(output_path=tmpdir, size=(width, height))
        paths = hti.screenshot(html_str=html, save_as="shot.png")
        png = Path(paths[0]).read_bytes()

    _store(key, png)
    return png


async def screenshot_to_file(
    html: str,
    dest: Path | str,
    *,
    width: int = 1200,
    height: int = 800,
) -> Path:
    """Like ``screenshot`` but writes to *dest* and returns the path."""
    png = await screenshot(html, width=width, height=height)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    return dest
