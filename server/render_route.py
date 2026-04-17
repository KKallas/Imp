"""server/render_route.py — standalone render server.

A lightweight FastAPI app that serves rendered charts without auth.
Runs on its own port (default 8421) alongside Chainlit.

URL contract::

    GET /render/<type>?var1=val&var2=val             → image/png
    GET /render/<type>?var1=val&var2=val&mode=viewer  → text/html
    GET /health                                       → 200 OK

Start standalone::

    python -m server.render_route          # port 8421
    python -m server.render_route --port 9000

Or let Chainlit spawn it automatically via ``start_background()``
in main.py — it launches a subprocess and stores the URL in
``cl.user_session``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure project root is importable when run as ``python -m server.render_route``
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

import renderers as _renderers

DEFAULT_PORT = int(os.environ.get("RENDER_PORT", "8421"))

app = FastAPI(title="Imp Render Server", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])


# ── helpers ─────────────────────────────────────────────────────────

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


# ── routes ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    plugins = list(_renderers.discover().keys())
    return {"status": "ok", "renderers": plugins}


@app.get("/render/{renderer_name}")
async def handle_render(request: Request, renderer_name: str) -> Response:
    mode = request.query_params.get("mode", "image")

    plugin = _renderers.get(renderer_name)
    if plugin is None:
        available = ", ".join(sorted(_renderers.discover()))
        return Response(
            f"Unknown renderer {renderer_name!r}.  Available: {available}",
            status_code=404,
        )

    params: dict[str, Any] = dict(request.query_params)
    params.pop("mode", None)

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
            f"[render] {renderer_name}.parse() failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return Response(f"Parse error: {exc}", status_code=400)

    try:
        html = _render_template(renderer_name, context)
    except Exception as exc:
        print(
            f"[render] template render failed for {renderer_name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return Response(f"Template error: {exc}", status_code=500)

    if mode == "viewer":
        return HTMLResponse(html)

    # Default: screenshot mode → PNG.
    from server.screenshot import available as _pw_available, screenshot

    if not _pw_available():
        return HTMLResponse(
            html,
            headers={"X-Render-Fallback": "playwright-not-installed"},
        )

    try:
        png = await screenshot(html)
    except Exception as exc:
        print(
            f"[render] screenshot failed for {renderer_name!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return HTMLResponse(
            html,
            headers={"X-Render-Fallback": "screenshot-failed"},
        )

    return Response(png, media_type="image/png")


# ── subprocess helper (called from main.py) ─────────────────────────

def start_background(port: int = DEFAULT_PORT) -> str:
    """Spawn the render server as a detached subprocess.

    Returns the base URL (e.g. ``http://127.0.0.1:8421``).
    """
    import subprocess

    subprocess.Popen(
        [sys.executable, "-m", "server.render_route", "--port", str(port)],
        cwd=str(_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return f"http://127.0.0.1:{port}"


# ── CLI entrypoint ──────────────────────────────────────────────────

def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Imp render server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    print(f"[render] starting on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
