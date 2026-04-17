"""server/render_route.py — ``/render/<type>`` HTTP route.

Serves rendered charts as either interactive HTML (``mode=viewer``) or
PNG screenshots (default).  Register by calling ``mount(app)`` with the
Starlette/Chainlit application.

URL contract::

    GET /render/<type>?var1=val&var2=val             → image/png
    GET /render/<type>?var1=val&var2=val&mode=viewer  → text/html
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import renderers as _renderers

_ROOT = Path(__file__).resolve().parent.parent


def _render_template(renderer_name: str, context: dict[str, Any]) -> str:
    """Load the plugin's ``template.html.j2`` and render it."""
    plugin = _renderers.get(renderer_name)
    if plugin is None:
        raise ValueError(f"unknown renderer: {renderer_name!r}")
    tmpl_path = plugin.template_path()
    if not tmpl_path.exists():
        raise FileNotFoundError(f"template not found: {tmpl_path}")
    env = Environment(
        loader=FileSystemLoader(str(tmpl_path.parent)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(tmpl_path.name)
    return template.render(**context)


async def handle_render(request: Request) -> Response:
    """Route handler for ``GET /render/{renderer_name}``."""
    renderer_name = request.path_params["renderer_name"]
    mode = request.query_params.get("mode", "image")

    plugin = _renderers.get(renderer_name)
    if plugin is None:
        available = ", ".join(sorted(_renderers.discover()))
        return Response(
            f"Unknown renderer {renderer_name!r}.  Available: {available}",
            status_code=404,
        )

    # Build template context from query params.
    params: dict[str, Any] = dict(request.query_params)
    params.pop("mode", None)

    # If the plugin uses a data/figure/diagram key, try to JSON-decode it.
    for key in ("data", "figure", "figure_json"):
        if key in params:
            try:
                params[key] = json.loads(params[key])
            except (json.JSONDecodeError, TypeError):
                pass

    try:
        context = plugin.parse(params)
    except Exception as exc:
        print(
            f"[render_route] {renderer_name}.parse() failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return Response(f"Parse error: {exc}", status_code=400)

    try:
        html = _render_template(renderer_name, context)
    except Exception as exc:
        print(
            f"[render_route] template render failed for {renderer_name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return Response(f"Template error: {exc}", status_code=500)

    if mode == "viewer":
        return HTMLResponse(html)

    # Default: screenshot mode — return PNG.
    from server.screenshot import available as _pw_available, screenshot

    if not _pw_available():
        # Fallback: return the HTML directly with a hint header.
        return HTMLResponse(
            html,
            headers={"X-Render-Fallback": "playwright-not-installed"},
        )

    try:
        png = await screenshot(html)
    except Exception as exc:
        print(
            f"[render_route] screenshot failed for {renderer_name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return HTMLResponse(
            html,
            headers={"X-Render-Fallback": "screenshot-failed"},
        )

    return Response(png, media_type="image/png")


def mount(app: Any) -> None:
    """Register the ``/render/{renderer_name}`` route on *app*."""
    app.add_route("/render/{renderer_name}", handle_render, methods=["GET"])
