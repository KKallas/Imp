"""server/render_route.py — standalone render server.

A lightweight FastAPI app that serves rendered charts without auth.
Runs on its own port (default 8421). Also serves the chat UI.

URL contract::

    GET /render/<type>?var1=val&var2=val               → image/png (5 s animation delay)
    GET /render/<type>?var1=val&var2=val&delay=0        → image/png (immediate)
    GET /render/<type>?var1=val&var2=val&delay=10000    → image/png (10 s delay)
    GET /render/<type>?var1=val&var2=val&mode=viewer    → text/html (interactive)
    GET /health                                       → 200 OK

Start standalone::

    python -m server.render_route          # port 8421
    python -m server.render_route --port 9000

Can also be spawned as a background subprocess via ``start_background()``.
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

from contextlib import asynccontextmanager as _acm

@_acm
async def _lifespan(app):
    # Startup: resume interrupted workflows
    try:
        import workflows
        await workflows.resume_paused_async()
    except Exception as exc:
        print(f"[render] workflow resume failed: {exc}", file=sys.stderr)
    yield

app = FastAPI(title="Imp Render Server", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])



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


@app.get("/api/version")
async def version():
    """Return the newest mtime across all server/pipeline/renderer files."""
    from datetime import datetime, timezone
    newest = 0.0
    for pattern in ("server/*.py", "pipeline/*.py", "renderers/**/*.py", "chat.html"):
        for p in _ROOT.glob(pattern):
            mt = p.stat().st_mtime
            if mt > newest:
                newest = mt
    ts = datetime.fromtimestamp(newest, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {"version": ts}


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
    delay_ms = int(params.pop("delay", 5000))  # animation wait (ms)

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
        png = await screenshot(html, delay_ms=delay_ms)
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


# ── chat UI routes ──────────────────────────────────────────────────

from starlette.responses import FileResponse
from starlette.websockets import WebSocket

_CHAT_HTML = _ROOT / "chat.html"


@app.get("/")
async def serve_chat_ui():
    """Serve the single-file chat UI (no caching)."""
    if _CHAT_HTML.exists():
        return FileResponse(
            _CHAT_HTML,
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return Response("chat.html not found", status_code=404)


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    from server.chat_ws import handle_ws_chat
    await handle_ws_chat(ws)


@app.get("/api/chats")
async def list_chats():
    from server import chat_history
    rows = chat_history.list_sessions(limit=50)
    return [
        {
            "id": r["id"],
            "title": r.get("title", "New chat"),
            "created_at": r.get("created_at", ""),
            "turn_count": r.get("turn_count", 0),
        }
        for r in rows
    ]


@app.post("/api/chats")
async def create_chat():
    from server import chat_history
    session = chat_history.ChatSession.new()
    chat_history.save_session(session)
    return {"id": session.id, "title": session.title}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    from server import chat_history
    session = chat_history.load_session(chat_id)
    if session is None:
        return Response("not found", status_code=404)
    return session.to_dict()


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    from server import chat_history
    ok = chat_history.delete_session(chat_id)
    return {"deleted": ok}


# ── queue API ───────────────────────────────────────────────────────

@app.get("/api/queue")
async def list_queue():
    from server import work_queue as queue
    return queue.list_pending()


@app.post("/api/queue")
async def add_to_queue(request: Request):
    from server import work_queue as queue
    data = await request.json()
    item = queue.add(
        tool=data.get("tool", "general"),
        title=data.get("title", ""),
        detail_html=data.get("detail_html", ""),
        actions=data.get("actions"),
    )
    return item


@app.post("/api/queue/{item_id}/action")
async def resolve_queue_item(item_id: str, request: Request):
    from server import work_queue as queue
    data = await request.json()
    item = queue.resolve(item_id, data.get("action", "done"))
    if item is None:
        return Response("not found", status_code=404)
    # Resume workflow if this item belongs to one
    tool = item.get("tool", "")
    if tool.startswith("workflow:"):
        import workflows
        wf_name = tool.split(":", 1)[1]
        runner = workflows.get_runner(wf_name)
        if runner and runner.status == "paused":
            runner.resume()
    return item


@app.delete("/api/queue/{item_id}")
async def delete_queue_item(item_id: str):
    from server import work_queue as queue
    return {"deleted": queue.remove(item_id)}


# ── workflow API ────────────────────────────────────────────────────

@app.get("/api/workflows")
async def list_workflows():
    import workflows
    discovered = workflows.discover()
    result = []
    runners = workflows.list_runners()
    for name, path in sorted(discovered.items()):
        readme = workflows.get_readme(name)
        first_line = readme.strip().split("\n")[0].lstrip("# ").strip() if readme else name
        steps = workflows.get_steps(name)
        runner_state = runners.get(name, {"status": "idle"})
        last_run = workflows.WorkflowRunner.load_last_run(name)
        ran_at = runner_state.get("ran_at") or (last_run.get("ran_at") if last_run else None)
        result.append({
            "name": name,
            "description": first_line,
            "step_count": len(steps),
            "status": runner_state.get("status", "idle") if name in runners else (last_run.get("status", "idle") if last_run else "idle"),
            "current_step": runner_state.get("current_step", 0),
            "ran_at": ran_at,
        })

    import tools as _tools
    tool_list = []
    for tname in sorted(_tools.discover()):
        for exe in _tools.list_executables(tname):
            desc = ""
            try:
                src = open(exe["script"]).read()
                for line in src.splitlines():
                    l = line.strip()
                    if l.startswith('"""') or l.startswith("'''"):
                        desc = l.strip('"').strip("'").strip()
                        break
            except Exception:
                pass
            tool_list.append({"group": tname, "name": exe["name"], "description": desc or exe["name"], "script": exe["script"]})

    return {"workflows": result, "tools": tool_list}


@app.post("/api/workflows/{name}/start")
async def start_workflow(name: str):
    # Clear previous run results so UI starts clean
    last_run = _ROOT / "workflows" / name / "last_run.json"
    if last_run.exists():
        last_run.unlink()
    import workflows
    runner = workflows.start(name)
    if runner is None:
        return Response(f"workflow {name!r} not found", status_code=404)
    return runner.to_dict()


@app.get("/api/workflows/{name}")
async def workflow_status(name: str):
    import workflows
    readme = workflows.get_readme(name)
    runner = workflows.get_runner(name)
    if runner is not None:
        d = runner.to_dict()
        d["readme"] = readme
        return d
    # No active runner — return steps + last run log if available
    last_run = workflows.WorkflowRunner.load_last_run(name)
    steps = workflows.get_steps(name)
    if last_run:
        for i, s in enumerate(steps):
            lr_steps = last_run.get("steps", [])
            if i < len(lr_steps) and lr_steps[i].get("result"):
                r = lr_steps[i]["result"]
                s["result"] = r
                if r.get("pause"):
                    s["status"] = "done"  # pause steps completed (were resolved)
                elif r.get("ok") is False:
                    s["status"] = "error"
                else:
                    s["status"] = "done"
            else:
                s["status"] = "pending"
        return {
            "name": name, "status": last_run.get("status", "idle"),
            "steps": steps, "ran_at": last_run.get("ran_at"), "readme": readme,
        }
    return {"name": name, "status": "idle", "steps": steps, "readme": readme}


@app.post("/api/workflows/{name}/abort")
async def abort_workflow(name: str):
    import workflows
    runner = workflows.get_runner(name)
    if runner is None:
        return Response("not running", status_code=404)
    runner.abort()
    return runner.to_dict()


@app.post("/api/workflows/{name}/delete")
async def delete_workflow(name: str):
    import shutil
    wf_dir = _ROOT / "workflows" / name
    if not wf_dir.is_dir():
        return Response("not found", status_code=404)
    shutil.rmtree(wf_dir)
    # Purge cached modules so re-creating with same name starts fresh
    import sys as _sys
    stale = [k for k in _sys.modules if k.startswith(f"step_") or k.startswith(f"workflows.{name}")]
    for k in stale:
        del _sys.modules[k]
    return {"deleted": name}


@app.post("/api/workflows/{name}/clone")
async def clone_workflow(name: str, request: Request):
    import shutil
    data = await request.json()
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return Response("new_name required", status_code=400)
    src = _ROOT / "workflows" / name
    dst = _ROOT / "workflows" / new_name
    if not src.is_dir():
        return Response("not found", status_code=404)
    if dst.exists():
        return Response("already exists", status_code=409)
    shutil.copytree(src, dst)
    lr = dst / "last_run.json"
    if lr.exists():
        lr.unlink()
    return {"cloned": new_name}


@app.post("/api/workflows/{name}/rename")
async def rename_workflow(name: str, request: Request):
    data = await request.json()
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return Response("new_name required", status_code=400)
    src = _ROOT / "workflows" / name
    dst = _ROOT / "workflows" / new_name
    if not src.is_dir():
        return Response("not found", status_code=404)
    if dst.exists():
        return Response("already exists", status_code=409)
    src.rename(dst)
    return {"renamed": new_name}


@app.post("/api/workflows/{name}/add-step")
async def add_step(name: str, request: Request):
    import re
    data = await request.json()
    tool_group = data.get("tool_group", "")
    tool_name = data.get("tool_name", "")
    wf_dir = _ROOT / "workflows" / name
    if not wf_dir.is_dir():
        wf_dir.mkdir(parents=True)
    existing = sorted(wf_dir.glob("step_*.py"))
    next_num = len(existing) + 1
    step_file = wf_dir / f"step_{next_num}_{tool_name}.py"
    tool_desc = tool_name
    tool_script = f"tools/{tool_group}/{tool_name}.py"
    import tools as _tools
    for exe in _tools.list_executables(tool_group):
        if exe["name"] == tool_name:
            try:
                src = open(exe["script"]).read()
                m = re.match(r'^(?:#!/.*\n)?(?:#.*\n)*\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')', src, re.DOTALL)
                if m:
                    tool_desc = (m.group(1) or m.group(2)).strip().split('\n')[0]
            except Exception:
                pass
            tool_script = exe["script"]
            break
    # Use step template if it exists, otherwise generate generic code
    template_file = _ROOT / "tools" / tool_group / f"{tool_name}.step.py"
    if template_file.exists():
        code = template_file.read_text()
    else:
        code = f'"""{tool_desc}"""\n\nimport subprocess\n\n\ndef run(context):\n    result = subprocess.run(\n        ["python", "{tool_script}"],\n        capture_output=True, text=True,\n    )\n    return {{\n        "ok": result.returncode == 0,\n        "output": result.stdout[:2000] or result.stderr[:2000],\n    }}\n'
    step_file.write_text(code)
    # Clear stale run results — step structure changed
    last_run = wf_dir / "last_run.json"
    if last_run.exists():
        last_run.unlink()
    return {"added": step_file.name}


@app.post("/api/workflows/{name}/remove-step")
async def remove_step(name: str, request: Request):
    data = await request.json()
    step_name = data.get("step_name", "").strip()
    wf_dir = _ROOT / "workflows" / name
    step_file = wf_dir / f"{step_name}.py"
    if step_file.exists():
        step_file.unlink()
        # Clear stale run results — step structure changed
        last_run = wf_dir / "last_run.json"
        if last_run.exists():
            last_run.unlink()
        _renumber_steps(wf_dir)
        return {"removed": step_name}
    return Response("step not found", status_code=404)


@app.post("/api/workflows/{name}/move-step")
async def move_step(name: str, request: Request):
    data = await request.json()
    step_name = data.get("step_name", "")
    direction = data.get("direction", "")
    wf_dir = _ROOT / "workflows" / name
    steps = sorted(wf_dir.glob("step_*.py"))
    names = [s.stem for s in steps]
    if step_name not in names:
        return Response("step not found", status_code=404)
    idx = names.index(step_name)
    if direction == "up" and idx > 0:
        steps[idx].rename(wf_dir / "tmp_swap.py")
        steps[idx - 1].rename(steps[idx])
        (wf_dir / "tmp_swap.py").rename(steps[idx - 1])
    elif direction == "down" and idx < len(steps) - 1:
        steps[idx].rename(wf_dir / "tmp_swap.py")
        steps[idx + 1].rename(steps[idx])
        (wf_dir / "tmp_swap.py").rename(steps[idx + 1])
    _renumber_steps(wf_dir)
    return {"moved": step_name, "direction": direction}


@app.post("/api/workflows/{name}/save-readme")
async def save_readme(name: str, request: Request):
    data = await request.json()
    content = data.get("content", "")
    readme = _ROOT / "workflows" / name / "README.md"
    readme.write_text(content)
    return {"saved": True}


@app.get("/api/tool-source")
async def tool_source(group: str, name: str):
    import re
    import tools as _tools
    for exe in _tools.list_executables(group):
        if exe["name"] == name:
            try:
                src = open(exe["script"]).read()
                m = re.match(r'^(?:#!/.*\n)?(?:#.*\n)*\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')', src, re.DOTALL)
                docstring = (m.group(1) or m.group(2)).strip() if m else ""
                return {"source": src, "docstring": docstring}
            except Exception:
                return {"source": "", "docstring": ""}
    return {"source": "", "docstring": ""}


@app.post("/api/workflows/{name}/configure")
async def configure_workflow(name: str):
    """Use Claude to update step Python code based on README + step descriptions."""
    import re
    import workflows

    wf_dir = _ROOT / "workflows" / name
    if not wf_dir.is_dir():
        return Response("not found", status_code=404)

    readme = workflows.get_readme(name)
    steps = workflows.get_steps(name)

    if not steps:
        return {"configured": 0, "message": "No steps to configure"}

    # Build step summary for context
    step_summary = "\n".join(f"  Step {i+1}: {s.get('description', s['name'])}" for i, s in enumerate(steps))

    configured = 0
    for i, step in enumerate(steps):
        src = step.get("source", "")
        desc = step.get("description", "")
        if not desc:
            continue

        prev_steps = "\n".join(f"  Step {j+1}: {s.get('description', s['name'])}" for j, s in enumerate(steps[:i]))

        prompt = f"""Update this workflow step's Python code so it actually does what the workflow needs.

WORKFLOW GOAL (from README):
{readme}

ALL STEPS IN THIS WORKFLOW:
{step_summary}

THIS IS STEP {i+1}: {desc}
{f"PREVIOUS STEPS (their output is in context['previous_results']):" + chr(10) + prev_steps if prev_steps else "This is the first step."}

CURRENT CODE:
```python
{src}
```

INSTRUCTIONS:
- The code must implement what step {i+1} needs to do FOR THIS SPECIFIC WORKFLOW
- PRESERVE the existing code structure — improve it, don't rewrite from scratch
- Previous step results are in context["previous_results"] — each is a dict with structured keys (e.g. "issue_number", "issue_title", "output", "ok"), NOT just a string. Use dict keys directly, never parse strings with regex.
- Check the CURRENT CODE carefully — if it already returns structured keys or reads them from context, keep that pattern
- Pass the right CLI arguments to the tool (check the current code for the correct flags)
- Use subprocess.run with the actual tool script path (keep the path from current code)
- Return {{"ok": bool, "output": str}} plus any structured keys that later steps might need
- Keep the docstring as: \"\"\"{desc}\"\"\"
- Use real values based on the workflow README, not placeholder/generic calls
- For dates use: from datetime import datetime; datetime.now().strftime(...)

Return ONLY the Python code. No explanation. No markdown fences."""

        print(f"\n[configure] === Step {i+1}: {desc} ===", file=sys.stderr)
        print(f"[configure] Prompt length: {len(prompt)} chars", file=sys.stderr)

        try:
            from claude_agent_sdk import ClaudeAgentOptions, query, TextBlock

            options = ClaudeAgentOptions(
                system_prompt="You are a code generator. Return only Python code, nothing else.",
                max_turns=1,
            )

            chunks = []
            async for message in query(prompt=prompt, options=options):
                from claude_agent_sdk import AssistantMessage
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)

            new_code = "".join(chunks).strip()
            print(f"[configure] LLM response ({len(new_code)} chars):", file=sys.stderr)
            print(new_code[:500], file=sys.stderr)
            if len(new_code) > 500:
                print("...(truncated)", file=sys.stderr)

            # Strip markdown fences if Claude added them
            if new_code.startswith("```python"):
                new_code = new_code[len("```python"):].strip()
            if new_code.startswith("```"):
                new_code = new_code[3:].strip()
            if new_code.endswith("```"):
                new_code = new_code[:-3].strip()

            if new_code and "def run" in new_code:
                step_file = Path(step["file"])
                step_file.write_text(new_code + "\n")
                configured += 1
                print(f"[configure] {name}/{step['name']}: updated", file=sys.stderr)
            else:
                print(f"[configure] {name}/{step['name']}: LLM returned invalid code, skipped", file=sys.stderr)

        except Exception as exc:
            print(f"[configure] {name}/{step['name']}: error: {exc}", file=sys.stderr)
            return {"error": str(exc), "configured": configured}

    return {"configured": configured, "message": f"Updated {configured} of {len(steps)} steps"}


def _renumber_steps(wf_dir: Path) -> None:
    import re
    steps = sorted(wf_dir.glob("step_*.py"))
    for i, step in enumerate(steps):
        m = re.match(r"step_\d+_(.*)", step.stem)
        suffix = m.group(1) if m else step.stem
        new_name = f"step_{i+1}_{suffix}.py"
        if step.name != new_name:
            step.rename(wf_dir / new_name)



# ── subprocess helper ───────────────────────────────────────────────

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
