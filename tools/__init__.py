"""tools — tool discovery + CRUD lifecycle for executables and their configs.

Each tool is a folder under ``tools/``.  Every ``.py`` file in the folder
(except ``__init__.py``) is an **executable** — a runnable script.  Each
executable can have a matching ``.md`` file as its prompt/config (the
"stored" part that gets CRUD'd by the admin via Foreman).

Discovery scans for ``tools/*/`` directories.  CRUD operations manage
the ``.md`` config files.  The reserved names ``new`` and ``delete``
cannot be used as executable names.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).parent
_RESERVED_NAMES = frozenset({"new", "delete", "list", "run", "edit"})
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


# ── discovery ───────────────────────────────────────────────────────

def discover() -> dict[str, Path]:
    """Return ``{tool_name: tool_dir}`` for every tool folder."""
    found: dict[str, Path] = {}
    for subdir in sorted(_TOOLS_DIR.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith(("_", ".")):
            continue
        # A tool folder has at least one .py file
        if any(subdir.glob("*.py")):
            found[subdir.name] = subdir
    return found


def list_executables(tool_name: str) -> list[dict[str, Any]]:
    """List all executables (.py files) in a tool folder."""
    d = _TOOLS_DIR / tool_name
    if not d.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.py")):
        if path.name == "__init__.py":
            continue
        name = path.stem
        # Check for matching .md config
        config_path = d / f"{name}.md"
        has_config = config_path.exists()
        results.append({
            "name": name,
            "script": str(path),
            "has_config": has_config,
            "config": str(config_path) if has_config else None,
        })
    return results


# ── config CRUD (.md files) ─────────────────────────────────────────

def _validate_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("name cannot be empty")
    if name.endswith(".md"):
        name = name[:-3]
    if name.endswith(".py"):
        name = name[:-3]
    if name in _RESERVED_NAMES:
        raise ValueError(f"{name!r} is a reserved operation name")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r} — use alphanumeric, hyphens, underscores"
        )
    return name


def read_config(tool_name: str, exec_name: str) -> str | None:
    """Read an executable's .md config. Returns None if not found."""
    exec_name = _validate_name(exec_name)
    path = _TOOLS_DIR / tool_name / f"{exec_name}.md"
    if not path.exists():
        return None
    return path.read_text()


def new_config(tool_name: str, exec_name: str, content: str) -> Path:
    """Create a new .md config for an executable. Raises if exists."""
    exec_name = _validate_name(exec_name)
    d = _TOOLS_DIR / tool_name
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{exec_name}.md"
    if path.exists():
        raise FileExistsError(f"{exec_name}.md already exists in {tool_name}")
    path.write_text(content)
    return path


def edit_config(tool_name: str, exec_name: str, content: str) -> Path:
    """Overwrite an existing .md config. Raises if not found."""
    exec_name = _validate_name(exec_name)
    path = _TOOLS_DIR / tool_name / f"{exec_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"{exec_name}.md not found in {tool_name}")
    path.write_text(content)
    return path


def delete_config(tool_name: str, exec_name: str) -> bool:
    """Delete a .md config. Returns True if deleted."""
    exec_name = _validate_name(exec_name)
    path = _TOOLS_DIR / tool_name / f"{exec_name}.md"
    if not path.exists():
        return False
    path.unlink()
    return True


# ── prompt generation ───────────────────────────────────────────────

def build_tool_list_for_prompt() -> str:
    """Auto-generate the 'Tools available' section for the system prompt.

    Scans ``tools/*/`` and lists every executable with its README
    description if available.
    """
    lines: list[str] = ["## Available tools\n"]
    lines.append("Try these tool scripts FIRST before using raw `gh` or Bash.")
    lines.append("Run them with: `python tools/<folder>/<script>.py --args`\n")

    for name, path in sorted(discover().items()):
        # Try to get description from README
        readme = path / "README.md"
        desc = ""
        if readme.exists():
            first_line = readme.read_text().strip().split("\n")[0]
            desc = f" — {first_line.lstrip('# ').strip()}"
        lines.append(f"### {name}/{desc}")

        for e in list_executables(name):
            cfg = " (has config)" if e["has_config"] else ""
            lines.append(f"- `{e['name']}`{cfg}")
        lines.append("")

    return "\n".join(lines)


def all_tool_paths() -> list[str]:
    """Return all tool script paths for whitelist generation.

    Used by ``intercept.py`` to auto-populate the command whitelist.
    """
    paths: list[str] = []
    for name in discover():
        for e in list_executables(name):
            paths.append(f"tools/{name}/{e['name']}.py")
    return paths
