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

_DEFAULT_DELAY_MS = 5000  # 5 s — enough for mermaid / Chart.js / Plotly animations


async def screenshot(
    html: str,
    *,
    width: int = 1200,
    height: int = 800,
    delay_ms: int = _DEFAULT_DELAY_MS,
) -> bytes:
    """Render *html* in headless Chrome and return PNG bytes.

    *delay_ms* controls how long Chrome waits for JS animations to
    finish before capturing (via ``--virtual-time-budget``).  Default
    is 5 000 ms.  Set to 0 to screenshot immediately.

    Results are cached by content hash — identical HTML always
    returns the cached image without a browser round-trip.
    """
    key = _cache_key(html)
    hit = _cached(key)
    if hit is not None:
        return hit

    from html2image import Html2Image

    flags: list[str] = []
    if delay_ms > 0:
        flags.append(f"--virtual-time-budget={delay_ms}")

    with tempfile.TemporaryDirectory() as tmpdir:
        hti = Html2Image(
            output_path=tmpdir,
            size=(width, height),
            custom_flags=flags or None,
        )
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
    delay_ms: int = _DEFAULT_DELAY_MS,
) -> Path:
    """Like ``screenshot`` but writes to *dest* and returns the path."""
    png = await screenshot(html, width=width, height=height, delay_ms=delay_ms)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    return dest
