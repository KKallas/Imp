#!/usr/bin/env python3
"""Save workflows and tools as a reusable preset.

Inputs:
  --name: str — preset name.
  --description: str — what this preset does.
  --workflow: str (repeatable) — workflow names to include.
  --tool-group: str (repeatable) — tool group names to include.

Process: Copies the specified workflows and tool groups into
         .imp/presets/<name>/ with a manifest file.
Output: Prints the preset path and contents."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


def main() -> int:
    parser = argparse.ArgumentParser(description="Save an automation preset")
    parser.add_argument("--name", required=True, help="Preset name")
    parser.add_argument("--description", default="", help="What this preset does")
    parser.add_argument("--workflow", action="append", default=[], help="Workflow name to include")
    parser.add_argument("--tool-group", action="append", default=[], help="Tool group to include")
    args = parser.parse_args()

    preset_dir = PRESETS_DIR / args.name
    if preset_dir.exists():
        print(f"Preset '{args.name}' already exists. Delete it first or pick a different name.")
        return 1

    preset_dir.mkdir(parents=True)

    # Copy workflows
    wf_count = 0
    for wf_name in args.workflow:
        src = ROOT / "workflows" / wf_name
        if src.is_dir():
            dst = preset_dir / "workflows" / wf_name
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "last_run.json"))
            wf_count += 1
            print(f"  + workflow: {wf_name}")
        else:
            print(f"  ! workflow not found: {wf_name}")

    # Copy tool groups
    tg_count = 0
    for tg_name in args.tool_group:
        src = ROOT / "tools" / tg_name
        if src.is_dir():
            dst = preset_dir / "tools" / tg_name
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            tg_count += 1
            print(f"  + tools: {tg_name}")
        else:
            print(f"  ! tool group not found: {tg_name}")

    # Write manifest
    manifest = {
        "name": args.name,
        "description": args.description,
        "workflows": args.workflow,
        "tool_groups": args.tool_group,
        "workflow_count": wf_count,
        "tool_group_count": tg_count,
    }
    (preset_dir / "preset.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nPreset saved: {preset_dir}")
    print(f"  {wf_count} workflow(s), {tg_count} tool group(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
