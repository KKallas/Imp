#!/usr/bin/env python3
"""List all available tool scripts in the tools/ directory.

Inputs:
  --group: str — filter by tool group name (e.g. "github", "imp").
  --verbose: Show the first-line description from each script's docstring.

Process: Scans tools/ for subdirectories containing .py scripts (excluding
         __init__.py, __pycache__, and .step.py files), groups them by folder,
         and prints a summary.
Output: Prints a table of all available tools grouped by category."""
import argparse
import ast
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent  # tools/


def _get_docstring_summary(script: Path) -> str:
    """Extract the first line of a script's module docstring, or ''."""
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
        docstring = ast.get_docstring(tree)
        if docstring:
            return docstring.strip().split("\n")[0]
    except Exception:
        pass
    return ""


def _collect_tools() -> dict[str, list[Path]]:
    """Return {group_name: [script_paths]} for every tool group."""
    groups: dict[str, list[Path]] = {}
    for child in sorted(TOOLS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        scripts = sorted(
            f
            for f in child.iterdir()
            if f.suffix == ".py"
            and f.is_file()
            and not f.name.startswith("_")
            and not f.name.endswith(".step.py")
        )
        if scripts:
            groups[child.name] = scripts
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description="List all available tool scripts")
    parser.add_argument(
        "--group",
        default=None,
        help="Show only tools in this group (e.g. 'github', 'imp')",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show the one-line description from each script's docstring",
    )
    args = parser.parse_args()

    groups = _collect_tools()

    if args.group:
        key = args.group.lower()
        groups = {k: v for k, v in groups.items() if k.lower() == key}
        if not groups:
            print(f"No tool group named '{args.group}'.")
            return 1

    total = 0
    for group, scripts in groups.items():
        print(f"\n  {group}/")
        for script in scripts:
            name = script.stem
            line = f"    {name}"
            if args.verbose:
                summary = _get_docstring_summary(script)
                if summary:
                    line += f"  — {summary}"
            print(line)
            total += 1

    print(f"\n{total} tool(s) in {len(groups)} group(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
