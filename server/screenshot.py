"""server/screenshot.py — headless browser screenshot engine.

Renders an HTML page via Playwright and returns PNG bytes.  The browser
instance is pooled: one Chromium process stays warm across calls so
repeated screenshots don't pay a cold-launch penalty.

Screenshots are cached by a SHA-256 content hash of the rendered HTML —
identical content always returns the cached PNG without re-launching a
browser tab.

Dependencies
------------
``playwright`` must be installed (``pip install playwright``) **and**
its Chromium browser downloaded (``playwright install chromium``).
When Playwright is missing the module degrades gracefully — the
``available()`` helper returns ``False`` and ``screenshot()`` raises
``RuntimeError`` with install instructions.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _ROOT / ".imp" / "output" / "screenshots"

# ── browser pool ────────────────────────────────────────────────────
_pw: Any = None
_browser: Any = None
_lock = asyncio.Lock()


def available() -> bool:
    """Return True when Playwright is importable."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _ensure_browser() -> Any:
    """Start (or reuse) a headless Chromium instance."""
    global _pw, _browser  # noqa: PLW0603
    async with _lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed.  Run:\n"
                "  pip install playwright && playwright install chromium"
            ) from None
        _pw = await async_playwright().__aenter__()
        _browser = await _pw.chromium.launch()
        return _browser


async def shutdown() -> None:
    """Close the pooled browser (call on app teardown)."""
    global _pw, _browser  # noqa: PLW0603
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _pw is not None:
        await _pw.__aexit__(None, None, None)
        _pw = None


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
    """Render *html* in headless Chromium and return PNG bytes.

    Results are cached by content hash — identical HTML always
    returns the cached image without a browser round-trip.
    """
    key = _cache_key(html)
    hit = _cached(key)
    if hit is not None:
        return hit

    browser = await _ensure_browser()
    page = await browser.new_page(viewport={"width": width, "height": height})
    try:
        await page.set_content(html, wait_until="networkidle")
        png = await page.screenshot(full_page=True)
    finally:
        await page.close()

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
