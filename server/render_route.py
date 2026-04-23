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
async def configure_workflow(name: str, request: Request):
    """Use Claude to update step Python code — streams progress as JSON lines."""
    import re
    import workflows
    from starlette.responses import StreamingResponse

    # Accept optional user prompt
    user_prompt = ""
    try:
        data = await request.json()
        user_prompt = data.get("user_prompt", "")
    except Exception:
        pass

    wf_dir = _ROOT / "workflows" / name
    if not wf_dir.is_dir():
        return Response("not found", status_code=404)

    readme = workflows.get_readme(name)
    steps = workflows.get_steps(name)

    if not steps:
        return Response(json.dumps({"type": "done", "configured": 0, "total": 0}) + "\n",
                        media_type="application/x-ndjson")

    async def generate():
        step_summary = "\n".join(f"  Step {i+1}: {s.get('description', s['name'])}" for i, s in enumerate(steps))
        configured = 0

        for i, step in enumerate(steps):
            src = step.get("source", "")
            desc = step.get("description", "")
            if not desc:
                yield json.dumps({"type": "step_skip", "step": i + 1, "description": step["name"]}) + "\n"
                continue

            yield json.dumps({"type": "step_start", "step": i + 1, "description": desc}) + "\n"

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

Return ONLY the Python code. No explanation. No markdown fences.{chr(10) + chr(10) + "ADDITIONAL USER INSTRUCTIONS:" + chr(10) + user_prompt if user_prompt else ""}"""

            print(f"\n[configure] === Step {i+1}: {desc} ===", file=sys.stderr)

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

                # Strip markdown fences
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
                    yield json.dumps({"type": "step_done", "step": i + 1, "description": desc}) + "\n"
                else:
                    print(f"[configure] {name}/{step['name']}: LLM returned invalid code", file=sys.stderr)
                    yield json.dumps({"type": "step_error", "step": i + 1, "error": "LLM returned invalid code"}) + "\n"

            except Exception as exc:
                print(f"[configure] {name}/{step['name']}: error: {exc}", file=sys.stderr)
                yield json.dumps({"type": "step_error", "step": i + 1, "error": str(exc)}) + "\n"
                continue

        yield json.dumps({"type": "done", "configured": configured, "total": len(steps)}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ── tool editing endpoints ─────────────────────────────────────────


def _extract_tool_args(source: str) -> list[dict]:
    """Parse argparse add_argument() calls from tool source via AST."""
    import ast

    args_info: list[dict] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return args_info

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue

        # Extract positional flag names
        flags = []
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                flags.append(a.value)
        if not flags:
            continue

        # Extract keyword arguments
        info: dict = {"flags": flags}
        for kw in node.keywords:
            if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                info["default"] = kw.value.value
            elif kw.arg == "choices" and isinstance(kw.value, (ast.List, ast.Tuple)):
                info["choices"] = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            elif kw.arg == "required" and isinstance(kw.value, ast.Constant):
                info["required"] = kw.value.value
            elif kw.arg == "help" and isinstance(kw.value, ast.Constant):
                info["help"] = kw.value.value
            elif kw.arg == "action" and isinstance(kw.value, ast.Constant):
                info["action"] = kw.value.value
            elif kw.arg == "type" and isinstance(kw.value, ast.Name):
                info["type"] = kw.value.id
        args_info.append(info)

    return args_info


def _extract_docstring(source: str) -> str:
    """Extract module-level docstring from Python source."""
    import ast
    try:
        return ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        return ""


@app.get("/api/tool-group-readme")
async def tool_group_readme(group: str):
    """Return README.md content for a tool group folder."""
    readme = _ROOT / "tools" / group / "README.md"
    if readme.exists():
        return {"readme": readme.read_text()}
    return {"readme": ""}


@app.get("/api/tools")
async def list_tools():
    """List all tools grouped by folder with descriptions and args."""
    import tools as _tools
    result = []
    for group_name in sorted(_tools.discover()):
        for exe in _tools.list_executables(group_name):
            try:
                src = Path(exe["script"]).read_text()
                desc = _extract_docstring(src)
                args = _extract_tool_args(src)
            except Exception:
                desc, args = "", []
            result.append({
                "group": group_name,
                "name": exe["name"],
                "description": desc,
                "args": args,
            })
    return result


@app.post("/api/tool-group-readme-save")
async def save_tool_group_readme(request: Request):
    """Save README.md for a tool group."""
    data = await request.json()
    group = data.get("group", "")
    content = data.get("content", "")
    readme = _ROOT / "tools" / group / "README.md"
    if not (_ROOT / "tools" / group).is_dir():
        return Response("group not found", status_code=404)
    readme.write_text(content)
    return {"saved": True}


@app.post("/api/tool-group-copy")
async def copy_tool_group(request: Request):
    """Copy an entire tool group folder."""
    import shutil
    data = await request.json()
    group = data.get("group", "")
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return {"error": "new_name required"}
    src = _ROOT / "tools" / group
    dst = _ROOT / "tools" / new_name
    if not src.is_dir():
        return Response("group not found", status_code=404)
    if dst.exists():
        return {"error": f"'{new_name}' already exists"}
    shutil.copytree(src, dst)
    return {"copied": new_name}


@app.post("/api/tool-group-rename")
async def rename_tool_group(request: Request):
    """Rename a tool group folder."""
    data = await request.json()
    group = data.get("group", "")
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return {"error": "new_name required"}
    src = _ROOT / "tools" / group
    dst = _ROOT / "tools" / new_name
    if not src.is_dir():
        return Response("group not found", status_code=404)
    if dst.exists():
        return {"error": f"'{new_name}' already exists"}
    src.rename(dst)
    return {"renamed": new_name}


@app.post("/api/tool-group-delete")
async def delete_tool_group(request: Request):
    """Delete an entire tool group folder."""
    import shutil
    data = await request.json()
    group = data.get("group", "")
    group_dir = _ROOT / "tools" / group
    if not group_dir.is_dir():
        return Response("group not found", status_code=404)
    shutil.rmtree(group_dir)
    return {"deleted": group}


@app.post("/api/tool-copy")
async def copy_tool(request: Request):
    """Copy a tool script (and its .step.py template if any)."""
    import shutil
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return {"error": "new_name required"}

    src = _ROOT / "tools" / group / f"{name}.py"
    dst = _ROOT / "tools" / group / f"{new_name}.py"
    if not src.exists():
        return Response("tool not found", status_code=404)
    if dst.exists():
        return {"error": f"{new_name}.py already exists"}
    shutil.copy2(src, dst)
    # Copy .step.py template too
    src_tpl = _ROOT / "tools" / group / f"{name}.step.py"
    if src_tpl.exists():
        shutil.copy2(src_tpl, _ROOT / "tools" / group / f"{new_name}.step.py")
    # Copy .md config too
    src_md = _ROOT / "tools" / group / f"{name}.md"
    if src_md.exists():
        shutil.copy2(src_md, _ROOT / "tools" / group / f"{new_name}.md")
    return {"copied": f"{group}/{new_name}"}


@app.post("/api/tool-delete")
async def delete_tool(request: Request):
    """Delete a tool script (and its .step.py template and .md config)."""
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")

    tool_path = _ROOT / "tools" / group / f"{name}.py"
    if not tool_path.exists():
        return Response("tool not found", status_code=404)
    tool_path.unlink()
    # Also remove related files
    for ext in [".step.py", ".md"]:
        related = _ROOT / "tools" / group / f"{name}{ext}"
        if related.exists():
            related.unlink()
    return {"deleted": f"{group}/{name}"}


@app.get("/api/tool-detail")
async def tool_detail(group: str, name: str):
    """Full tool detail: source, docstring, parsed args, step template."""
    import tools as _tools
    for exe in _tools.list_executables(group):
        if exe["name"] == name:
            try:
                src = Path(exe["script"]).read_text()
                # Check for .step.py template
                step_tpl = _ROOT / "tools" / group / f"{name}.step.py"
                step_template = step_tpl.read_text() if step_tpl.exists() else ""
                return {
                    "group": group,
                    "name": name,
                    "source": src,
                    "docstring": _extract_docstring(src),
                    "args": _extract_tool_args(src),
                    "step_template": step_template,
                }
            except Exception:
                return {"group": group, "name": name, "source": "", "docstring": "", "args": [], "step_template": ""}
    return Response("tool not found", status_code=404)


@app.post("/api/tool-group-describe")
async def describe_tool_group(request: Request):
    """LLM generates a README for a tool group based on all its tool scripts."""
    data = await request.json()
    group = data.get("group", "")
    user_prompt = data.get("user_prompt", "")

    group_dir = _ROOT / "tools" / group
    if not group_dir.is_dir():
        return Response("group not found", status_code=404)

    # Collect all tool summaries
    import tools as _tools
    tool_summaries = []
    for exe in _tools.list_executables(group):
        try:
            src = Path(exe["script"]).read_text()
            doc = _extract_docstring(src)
            args = _extract_tool_args(src)
            arg_desc = ", ".join(a["flags"][0] for a in args) if args else "none"
            tool_summaries.append(f"- {exe['name']}.py: {doc or 'no description'} (args: {arg_desc})")
        except Exception:
            tool_summaries.append(f"- {exe['name']}.py: (could not read)")

    tools_text = "\n".join(tool_summaries)

    prompt = f"""Write a README.md for the "{group}" tool group folder.

This folder contains these tool scripts:
{tools_text}

The README should:
- Start with `# {group}` heading
- One-line summary of what this group does
- A table or list of all scripts with their purpose and key arguments
- Brief usage examples showing how to run the most common tools
- Keep it concise and practical — under 40 lines

Return ONLY the markdown content.{chr(10) + chr(10) + "ADDITIONAL USER INSTRUCTIONS:" + chr(10) + user_prompt if user_prompt else ""}"""

    print(f"[tool-group-describe] {group}", file=sys.stderr)

    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        options = ClaudeAgentOptions(
            system_prompt="You write concise README.md files. Return only markdown.",
            max_turns=1,
        )

        chunks = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)

        new_readme = "".join(chunks).strip()
        if not new_readme:
            return {"error": "LLM returned empty README"}

        readme_path = group_dir / "README.md"
        readme_path.write_text(new_readme + "\n")
        print(f"[tool-group-describe] {group}: README updated", file=sys.stderr)
        return {"updated": True}

    except Exception as exc:
        print(f"[tool-group-describe] error: {exc}", file=sys.stderr)
        return {"error": str(exc)}


@app.post("/api/tool-move")
async def move_tool(request: Request):
    """Move a tool (and its .step.py and .md) to a different group."""
    import shutil
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    new_group = data.get("new_group", "").strip()
    if not new_group:
        return {"error": "new_group required"}

    src_dir = _ROOT / "tools" / group
    dst_dir = _ROOT / "tools" / new_group
    if not src_dir.is_dir():
        return Response("source group not found", status_code=404)
    dst_dir.mkdir(parents=True, exist_ok=True)

    if (dst_dir / f"{name}.py").exists():
        return {"error": f"{name}.py already exists in {new_group}"}

    for ext in [".py", ".step.py", ".md"]:
        src = src_dir / f"{name}{ext}"
        if src.exists():
            shutil.move(str(src), str(dst_dir / f"{name}{ext}"))

    return {"moved": f"{new_group}/{name}"}


@app.post("/api/tool-describe-save")
async def save_tool_docstring(request: Request):
    """Save a tool's docstring without LLM — just update the text."""
    import ast as _ast
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    new_docstring = data.get("docstring", "").strip()

    tool_path = _ROOT / "tools" / group / f"{name}.py"
    if not tool_path.exists():
        return Response("tool not found", status_code=404)

    source = tool_path.read_text()
    try:
        tree = _ast.parse(source)
        first_node = tree.body[0] if tree.body else None
        if (first_node
            and isinstance(first_node, _ast.Expr)
            and isinstance(first_node.value, (_ast.Constant, _ast.Str))):
            start_line = first_node.lineno - 1
            end_line = first_node.end_lineno
            lines = source.splitlines(keepends=True)
            new_source = "".join(lines[:start_line]) + f'"""{new_docstring}"""\n' + "".join(lines[end_line:])
        else:
            import re
            header_match = re.match(r'^((?:#!/.*\n)?(?:#.*\n)*)', source)
            prefix = header_match.group(1) if header_match else ""
            rest = source[len(prefix):]
            new_source = prefix + f'"""{new_docstring}"""\n\n' + rest
    except SyntaxError:
        new_source = f'"""{new_docstring}"""\n\n' + source

    tool_path.write_text(new_source)
    return {"saved": True, "docstring": new_docstring}


@app.post("/api/tool-describe")
async def describe_tool(request: Request):
    """LLM generates a description from the tool's source code."""
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    user_prompt = data.get("user_prompt", "")

    tool_path = _ROOT / "tools" / group / f"{name}.py"
    if not tool_path.exists():
        return Response("tool not found", status_code=404)

    source = tool_path.read_text()

    extra = f"\n\nADDITIONAL USER INSTRUCTIONS:\n{user_prompt}" if user_prompt else ""
    prompt = f"""Write a short but descriptive docstring for this Python tool.

```python
{source}
```

The docstring must cover:
- What the tool does (one sentence)
- Inputs: list each CLI argument/flag, its type, and purpose
- Process: what it does internally
- Output: what it prints/returns

Keep it concise — no more than 8 lines. Return ONLY the docstring text (no triple quotes, no code).{extra}"""

    print(f"[tool-describe] {group}/{name}" + (f" (prompt: {user_prompt[:80]})" if user_prompt else ""), file=sys.stderr)

    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        options = ClaudeAgentOptions(
            system_prompt="You write concise Python docstrings. Return only the docstring text.",
            max_turns=1,
        )

        chunks = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)

        new_docstring = "".join(chunks).strip().strip('"').strip("'").strip()
        if not new_docstring:
            return {"error": "LLM returned empty description"}

        # Update the docstring in the file using AST for precise location
        import ast as _ast
        try:
            tree = _ast.parse(source)
            first_node = tree.body[0] if tree.body else None
            if (first_node
                and isinstance(first_node, _ast.Expr)
                and isinstance(first_node.value, (_ast.Constant, _ast.Str))):
                # Found existing module docstring — replace it precisely
                # AST gives 1-based line numbers
                start_line = first_node.lineno - 1  # 0-based
                end_line = first_node.end_lineno     # exclusive (1-based end = exclusive 0-based)
                lines = source.splitlines(keepends=True)
                new_source = "".join(lines[:start_line]) + f'"""{new_docstring}"""\n' + "".join(lines[end_line:])
            else:
                # No docstring — insert after shebang/comments
                import re
                header_match = re.match(r'^((?:#!/.*\n)?(?:#.*\n)*)', source)
                prefix = header_match.group(1) if header_match else ""
                rest = source[len(prefix):]
                new_source = prefix + f'"""{new_docstring}"""\n\n' + rest
        except SyntaxError:
            # Fallback: prepend
            new_source = f'"""{new_docstring}"""\n\n' + source

        tool_path.write_text(new_source)
        print(f"[tool-describe] {group}/{name}: docstring updated", file=sys.stderr)
        return {"updated": True, "docstring": new_docstring}

    except Exception as exc:
        print(f"[tool-describe] error: {exc}", file=sys.stderr)
        return {"error": str(exc)}


@app.post("/api/tool-edit")
async def edit_tool(request: Request):
    """LLM-powered tool code update based on description change.

    Updates the tool script and creates/updates the .step.py template.
    """
    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    new_description = data.get("new_description", "")
    user_prompt = data.get("user_prompt", "")

    tool_path = _ROOT / "tools" / group / f"{name}.py"
    if not tool_path.exists():
        return Response("tool not found", status_code=404)

    current_source = tool_path.read_text()
    current_docstring = _extract_docstring(current_source)

    # Check for existing step template
    tpl_path = _ROOT / "tools" / group / f"{name}.step.py"
    current_template = tpl_path.read_text() if tpl_path.exists() else ""

    # Build prompt — ask LLM to return both the tool script AND the workflow template
    tpl_section = ""
    if current_template:
        tpl_section = f"""
CURRENT WORKFLOW TEMPLATE ({name}.step.py):
```python
{current_template}
```"""
    else:
        tpl_section = f"""
There is NO workflow template yet. Create one as {name}.step.py.
A workflow template has `def run(context):` and calls the tool via subprocess.
context["previous_results"] contains results from earlier workflow steps."""

    prompt = f"""Update this tool based on the new description.

CURRENT DESCRIPTION:
{current_docstring}

NEW DESCRIPTION (user's instruction):
{new_description}

CURRENT TOOL SCRIPT ({name}.py):
```python
{current_source}
```
{tpl_section}

Return TWO code blocks separated by the exact line: ---TEMPLATE---

FIRST block: the updated tool script ({name}.py)
- PRESERVE the existing code structure (argparse, imports, main function, entry point)
- Keep the same CLI interface unless the description explicitly changes it
- Update the module docstring to match the new description
- Do NOT change the if __name__ == "__main__" pattern

SECOND block: the workflow template ({name}.step.py)
- Must have `def run(context):` (NOT def main)
- Call the tool via subprocess.run(["python", "tools/{group}/{name}.py", ...])
- Read from context["previous_results"] if needed
- Return {{"ok": bool, "output": str}} plus any structured keys

Return ONLY the two Python code blocks separated by ---TEMPLATE---. No explanation. No markdown fences.{chr(10) + chr(10) + "ADDITIONAL USER INSTRUCTIONS:" + chr(10) + user_prompt if user_prompt else ""}"""

    print(f"\n[tool-edit] {group}/{name}: editing...", file=sys.stderr)

    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

        options = ClaudeAgentOptions(
            system_prompt="You are a code generator. Return only Python code, nothing else.",
            max_turns=1,
        )

        chunks = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)

        raw = "".join(chunks).strip()

        # Strip outer markdown fences
        if raw.startswith("```python"):
            raw = raw[len("```python"):].strip()
        if raw.startswith("```"):
            raw = raw[3:].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        # Split on ---TEMPLATE---
        parts = raw.split("---TEMPLATE---")
        new_tool_code = parts[0].strip()
        new_tpl_code = parts[1].strip() if len(parts) > 1 else ""

        # Clean each part of markdown fences
        for fence in ["```python", "```"]:
            if new_tool_code.startswith(fence):
                new_tool_code = new_tool_code[len(fence):].strip()
            if new_tpl_code.startswith(fence):
                new_tpl_code = new_tpl_code[len(fence):].strip()
        if new_tool_code.endswith("```"):
            new_tool_code = new_tool_code[:-3].strip()
        if new_tpl_code.endswith("```"):
            new_tpl_code = new_tpl_code[:-3].strip()

        updated = {}

        # Write tool script
        if new_tool_code and ("def main" in new_tool_code or "if __name__" in new_tool_code):
            print(f"[tool-edit] {group}/{name}.py: updating", file=sys.stderr)
            tool_path.write_text(new_tool_code + "\n")
            updated["script"] = True
        else:
            print(f"[tool-edit] {group}/{name}.py: LLM returned invalid tool code, skipped", file=sys.stderr)

        # Write template
        if new_tpl_code and "def run" in new_tpl_code:
            print(f"[tool-edit] {group}/{name}.step.py: {'updating' if current_template else 'creating'}", file=sys.stderr)
            tpl_path.write_text(new_tpl_code + "\n")
            updated["template"] = True
        else:
            print(f"[tool-edit] {group}/{name}.step.py: LLM returned invalid template, skipped", file=sys.stderr)

        if not updated:
            return {"updated": False, "error": "LLM returned invalid code for both files"}

        return {"updated": True, **updated, "docstring": _extract_docstring(new_tool_code or current_source)}

    except Exception as exc:
        print(f"[tool-edit] error: {exc}", file=sys.stderr)
        return {"error": str(exc)}


@app.post("/api/tool-run")
async def run_tool(request: Request):
    """Debug execution: run a tool with user-provided arguments."""
    import asyncio

    data = await request.json()
    group = data.get("group", "")
    name = data.get("name", "")
    args = data.get("args", {})  # {"--repo": "KKallas/Imp", "--limit": "10"}

    tool_path = _ROOT / "tools" / group / f"{name}.py"
    if not tool_path.exists():
        return Response("tool not found", status_code=404)

    cmd = [sys.executable, str(tool_path)]
    for flag, value in args.items():
        if value is None or value == "":
            continue
        if flag.startswith("_pos_"):
            # Raw positional argument (from free-text input)
            cmd.append(str(value))
        elif isinstance(value, bool):
            if value:
                cmd.append(flag)
        else:
            cmd.append(flag)
            cmd.append(str(value))

    print(f"[tool-run] {group}/{name}: {' '.join(cmd)}", file=sys.stderr)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout.decode()[:5000],
            "stderr": stderr.decode()[:2000],
        }
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": "Timed out after 30 seconds"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _renumber_steps(wf_dir: Path) -> None:
    import re
    steps = sorted(wf_dir.glob("step_*.py"))
    for i, step in enumerate(steps):
        m = re.match(r"step_\d+_(.*)", step.stem)
        suffix = m.group(1) if m else step.stem
        new_name = f"step_{i+1}_{suffix}.py"
        if step.name != new_name:
            step.rename(wf_dir / new_name)



# ── developer sync endpoints ───────────────────────────────────────

_SYNC_DIRS = ["tools", "workflows", "renderers", "public"]
_SYNC_EXCLUDE = {"__pycache__", ".pyc", "last_run.json", ".DS_Store"}


def _sync_file_list() -> dict[str, dict]:
    """Build manifest of all syncable files with hashes and mtimes."""
    import hashlib
    files = {}
    for sync_dir in _SYNC_DIRS:
        base = _ROOT / sync_dir
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if f.name in _SYNC_EXCLUDE or any(p in _SYNC_EXCLUDE for p in f.parts):
                continue
            if f.suffix == ".pyc":
                continue
            rel = str(f.relative_to(_ROOT))
            try:
                content = f.read_bytes()
                files[rel] = {
                    "hash": hashlib.md5(content).hexdigest(),
                    "mtime": f.stat().st_mtime,
                    "size": len(content),
                }
            except OSError:
                pass
    return files


@app.get("/api/sync/manifest")
async def sync_manifest():
    """Return all syncable files with hashes and mtimes."""
    return {"files": _sync_file_list()}


@app.get("/api/sync/file")
async def sync_download(path: str):
    """Download a single file."""
    from fastapi.responses import PlainTextResponse
    full = _ROOT / path
    # Safety: must be under a sync dir
    if not any(path.startswith(d + "/") for d in _SYNC_DIRS):
        return Response("forbidden", status_code=403)
    if not full.is_file():
        return Response("not found", status_code=404)
    return PlainTextResponse(full.read_text(), media_type="text/plain")


@app.post("/api/sync/file")
async def sync_upload(request: Request):
    """Upload/update a single file."""
    data = await request.json()
    path = data.get("path", "")
    content = data.get("content", "")
    if not any(path.startswith(d + "/") for d in _SYNC_DIRS):
        return {"error": "forbidden — path must be under tools/, workflows/, renderers/, or public/"}
    full = _ROOT / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return {"uploaded": path}


@app.delete("/api/sync/file")
async def sync_delete(path: str):
    """Delete a file on the server."""
    if not any(path.startswith(d + "/") for d in _SYNC_DIRS):
        return {"error": "forbidden"}
    full = _ROOT / path
    if full.is_file():
        full.unlink()
        return {"deleted": path}
    return Response("not found", status_code=404)


@app.get("/imp-sync.py")
async def download_sync_script(request: Request):
    """Generate and serve the sync client script with server URL baked in."""
    from fastapi.responses import PlainTextResponse
    host = request.headers.get("host", "127.0.0.1:8421")
    server_url = f"http://{host}"
    script = _IMP_SYNC_SCRIPT.replace("{{SERVER_URL}}", server_url)
    return PlainTextResponse(script, media_type="text/plain",
                             headers={"Content-Disposition": "attachment; filename=imp-sync.py"})


_IMP_SYNC_SCRIPT = r'''#!/usr/bin/env python3
"""Imp Developer Sync — bidirectional file sync with Imp server.

Drop this file in any folder and run:  python imp-sync.py
First run pulls all files. Then watches for changes on both sides.
Ctrl+C to stop.

Zero dependencies — stdlib only.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER = "{{SERVER_URL}}"
POLL_INTERVAL = 2  # seconds
SYNC_DIRS = ["tools", "workflows", "renderers", "public"]
EXCLUDE = {"__pycache__", ".pyc", "last_run.json", ".DS_Store"}

_local_state = {}  # path -> {"hash", "mtime"}


def api(method, endpoint, data=None):
    """Make an API call to the Imp server."""
    url = f"{SERVER}{endpoint}"
    if data is not None:
        req = urllib.request.Request(
            url, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return json.loads(body)
            return body
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {endpoint}")
        return None
    except urllib.error.URLError as e:
        print(f"  Connection error: {e.reason}")
        return None


def file_hash(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def local_manifest():
    """Scan local files and return {rel_path: {hash, mtime}}."""
    files = {}
    for sync_dir in SYNC_DIRS:
        base = Path(sync_dir)
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if f.name in EXCLUDE or any(p in EXCLUDE for p in f.parts):
                continue
            if f.suffix == ".pyc":
                continue
            rel = str(f)
            try:
                files[rel] = {
                    "hash": file_hash(f),
                    "mtime": f.stat().st_mtime,
                }
            except OSError:
                pass
    return files


def pull_file(path):
    """Download a file from the server."""
    content = api("GET", f"/api/sync/file?path={urllib.request.quote(path)}")
    if content is None:
        return False
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _local_state[path] = {"hash": file_hash(p), "mtime": p.stat().st_mtime}
    return True


def push_file(path):
    """Upload a local file to the server."""
    p = Path(path)
    if not p.is_file():
        return False
    content = p.read_text()
    result = api("POST", "/api/sync/file", {"path": path, "content": content})
    if result and not result.get("error"):
        _local_state[path] = {"hash": file_hash(p), "mtime": p.stat().st_mtime}
        return True
    return False


def delete_remote(path):
    """Delete a file on the server."""
    api("DELETE", f"/api/sync/file?path={urllib.request.quote(path)}")


def delete_local(path):
    """Delete a local file."""
    p = Path(path)
    if p.is_file():
        p.unlink()
    _local_state.pop(path, None)


def initial_sync():
    """First sync — pull everything from server."""
    print(f"Connecting to {SERVER}...")
    manifest = api("GET", "/api/sync/manifest")
    if not manifest:
        print("Failed to connect. Is the server running?")
        sys.exit(1)

    remote_files = manifest.get("files", {})
    print(f"Server has {len(remote_files)} files")

    pulled = 0
    for path in remote_files:
        local = Path(path)
        if local.is_file() and file_hash(local) == remote_files[path]["hash"]:
            _local_state[path] = {"hash": remote_files[path]["hash"], "mtime": local.stat().st_mtime}
            continue
        print(f"  ↓ {path}")
        if pull_file(path):
            pulled += 1

    # Also register existing local files
    for path, info in local_manifest().items():
        if path not in _local_state:
            _local_state[path] = info

    print(f"Pulled {pulled} files. Watching for changes...\n")


def sync_loop():
    """Main sync loop — poll for changes on both sides."""
    while True:
        try:
            time.sleep(POLL_INTERVAL)

            # Get remote manifest
            manifest = api("GET", "/api/sync/manifest")
            if not manifest:
                continue
            remote = manifest.get("files", {})
            local = local_manifest()

            # Remote changes → pull
            for path, rinfo in remote.items():
                prev = _local_state.get(path)
                if prev and prev["hash"] == rinfo["hash"]:
                    continue  # no change
                linfo = local.get(path)
                if linfo and linfo["hash"] == rinfo["hash"]:
                    _local_state[path] = linfo
                    continue  # already in sync
                # Remote is newer or new file
                if linfo and prev and linfo["hash"] != prev["hash"]:
                    # Both changed — conflict
                    print(f"  ⚠ CONFLICT {path} — keeping both (remote as .conflict)")
                    conflict = Path(path + ".conflict")
                    if pull_file(path):
                        Path(path).rename(conflict)
                    continue
                print(f"  ↓ {path}")
                pull_file(path)

            # Remote deletions → delete local
            for path in list(_local_state.keys()):
                if path not in remote and path not in local:
                    continue
                if path not in remote and path in local:
                    # File was on server last time but now gone
                    prev = _local_state.get(path)
                    linfo = local.get(path)
                    if prev and linfo and linfo["hash"] == prev["hash"]:
                        # Local hasn't changed, safe to delete
                        print(f"  ✕ {path} (deleted on server)")
                        delete_local(path)

            # Local changes → push
            for path, linfo in local.items():
                prev = _local_state.get(path)
                if prev and prev["hash"] == linfo["hash"]:
                    continue  # no change
                rinfo = remote.get(path)
                if rinfo and rinfo["hash"] == linfo["hash"]:
                    _local_state[path] = linfo
                    continue  # already in sync
                print(f"  ↑ {path}")
                push_file(path)

            # Local deletions → delete remote
            for path in list(_local_state.keys()):
                if path in local or path in remote:
                    continue
                prev = _local_state.get(path)
                rinfo = remote.get(path)
                if prev and rinfo and rinfo["hash"] == prev["hash"]:
                    print(f"  ✕ {path} (deleted locally)")
                    delete_remote(path)
                    _local_state.pop(path, None)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  sync error: {e}")


def main():
    print("=" * 50)
    print("  Imp Developer Sync")
    print(f"  Server: {SERVER}")
    print("=" * 50)
    print()

    initial_sync()

    try:
        sync_loop()
    except KeyboardInterrupt:
        print("\nSync stopped.")


if __name__ == "__main__":
    main()
'''


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
