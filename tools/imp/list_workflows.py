#!/usr/bin/env python3
"""List all workflows with their README content and step counts.

Inputs:
  --name: str — show only this workflow (optional).
  --verbose: Show full README content instead of just the first line.

Process: Scans workflows/ for subdirectories with step scripts,
         reads each workflow's README.md for description.
Output: Prints workflow names, step counts, and descriptions."""
import argparse
import sys
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent.parent / "workflows"


def _get_steps(wf_dir: Path) -> list[str]:
    return sorted(f.stem for f in wf_dir.glob("step_*.py"))


def _get_readme(wf_dir: Path) -> str:
    readme = wf_dir / "README.md"
    if readme.exists():
        return readme.read_text().strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="List all workflows")
    parser.add_argument("--name", default=None, help="Show only this workflow")
    parser.add_argument("--verbose", action="store_true", help="Show full README")
    args = parser.parse_args()

    if not WORKFLOWS_DIR.is_dir():
        print("No workflows/ directory found.")
        return 1

    workflows = sorted(
        d for d in WORKFLOWS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(("_", "."))
    )

    if args.name:
        workflows = [d for d in workflows if d.name == args.name]
        if not workflows:
            print(f"Workflow '{args.name}' not found.")
            return 1

    for wf_dir in workflows:
        steps = _get_steps(wf_dir)
        readme = _get_readme(wf_dir)
        first_line = readme.split("\n")[0].lstrip("# ").strip() if readme else "(no description)"

        print(f"\n  {wf_dir.name}/  ({len(steps)} steps)")
        if args.verbose and readme:
            for line in readme.split("\n"):
                print(f"    {line}")
        else:
            print(f"    {first_line}")
        if steps:
            for s in steps:
                print(f"      - {s}")

    print(f"\n{len(workflows)} workflow(s) found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
