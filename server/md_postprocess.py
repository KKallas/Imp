"""server/md_postprocess.py — replace fenced code blocks with rendered images.

Scans assistant markdown output for fenced code blocks whose language
tag matches a registered renderer's ``block_type``.  Each matching
block is:

1. Parsed by the renderer plugin → template vars.
2. Rendered to HTML via the plugin's Jinja2 template.
3. Screenshotted to PNG (if Playwright is available).
4. Saved to ``public/images/<hash>.png``.
5. Replaced in the markdown with ``[![alt](image_url)](viewer_url)``.

If Playwright is unavailable, the original code block is left intact
and a markdown link to the interactive HTML viewer is appended instead.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_PUBLIC_IMAGES = _ROOT / "public" / "images"

# Match fenced code blocks: ```<lang>\n<content>\n```
_FENCE_RE = re.compile(
    r"^```(\w+)\s*\n(.*?)^```",
    re.MULTILINE | re.DOTALL,
)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def postprocess(
    text: str,
    *,
    base_url: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Scan *text* for renderable code blocks and replace them.

    Returns ``(new_text, elements)`` where *elements* is a list of
    dicts with keys ``image_path``, ``image_url``, ``viewer_url`` for
    each replaced block (Chainlit may use these to attach ``cl.Image``
    elements).
    """
    import renderers as _renderers

    plugins = _renderers.discover()
    block_map: dict[str, Any] = {
        p.block_type: p for p in plugins.values() if p.block_type
    }
    if not block_map:
        return text, []

    elements: list[dict[str, Any]] = []

    async def _replace(match: re.Match) -> str:
        lang = match.group(1)
        content = match.group(2).strip()

        plugin = block_map.get(lang)
        if plugin is None:
            return match.group(0)  # leave non-matching blocks alone

        try:
            context = plugin.parse(content)
        except Exception as exc:
            print(
                f"[md_postprocess] {plugin.name}.parse() failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return match.group(0)

        # Render template → HTML
        from server.render_route import _render_template

        try:
            html = _render_template(plugin.name, context)
        except Exception as exc:
            print(
                f"[md_postprocess] template render failed for "
                f"{plugin.name!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return match.group(0)

        slug = _content_hash(content)
        _, viewer_url = plugin.build_url(
            {"diagram": content} if plugin.block_type == "mermaid" else context,
            base=base_url,
        )

        # Try screenshot
        from server.screenshot import available as _pw_available

        if _pw_available():
            from server.screenshot import screenshot

            try:
                png = await screenshot(html)
                _PUBLIC_IMAGES.mkdir(parents=True, exist_ok=True)
                img_name = f"{plugin.name}_{slug}.png"
                img_path = _PUBLIC_IMAGES / img_name
                img_path.write_bytes(png)
                image_url = f"{base_url}/public/images/{img_name}"
                elements.append({
                    "image_path": str(img_path),
                    "image_url": image_url,
                    "viewer_url": viewer_url,
                })
                return f"[![{plugin.name} diagram]({image_url})]({viewer_url})"
            except Exception as exc:
                print(
                    f"[md_postprocess] screenshot failed for "
                    f"{plugin.name!r}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

        # Fallback: keep the block and append an interactive link
        return (
            match.group(0)
            + f"\n\n[Open interactive {plugin.name} viewer]({viewer_url})"
        )

    # re.sub doesn't support async; do manual iteration
    result_parts: list[str] = []
    last_end = 0
    for match in _FENCE_RE.finditer(text):
        result_parts.append(text[last_end : match.start()])
        replacement = await _replace(match)
        result_parts.append(replacement)
        last_end = match.end()
    result_parts.append(text[last_end:])

    return "".join(result_parts), elements
