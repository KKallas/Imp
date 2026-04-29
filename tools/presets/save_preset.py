#!/usr/bin/env python3
"""Save all workflows and tools as a preset with activation state.

Inputs:
  --name: str — preset name.
  --description: str — what this preset does.

Process: Copies all workflows and tool groups into .imp/presets/<name>/,
         saves which ones are currently active in the manifest.
Output: Prints the preset contents."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


def _load_config() -> dict:
    cfg_file = ROOT / ".imp" / "config.json"
    if cfg_file.exists():
        try:
            return json.loads(cfg_file.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Save an automation preset")
    parser.add_argument("--name", required=True, help="Preset name")
    parser.add_argument("--description", default="", help="What this preset does")
    args = parser.parse_args()

    preset_dir = PRESETS_DIR / args.name
    if preset_dir.exists():
        print(f"Preset '{args.name}' already exists. Delete it first or pick a different name.")
        return 1

    preset_dir.mkdir(parents=True)
    cfg = _load_config()

    # Copy all workflows
    wf_dir = ROOT / "workflows"
    wf_names = []
    if wf_dir.is_dir():
        for d in sorted(wf_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(("_", ".")):
                dst = preset_dir / "workflows" / d.name
                shutil.copytree(d, dst, ignore=shutil.ignore_patterns("__pycache__", "last_run.json"))
                wf_names.append(d.name)
                print(f"  + workflow: {d.name}")

    # Copy all tool groups
    tg_dir = ROOT / "tools"
    tg_names = []
    if tg_dir.is_dir():
        for d in sorted(tg_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(("_", ".")) and any(d.glob("*.py")):
                dst = preset_dir / "tools" / d.name
                shutil.copytree(d, dst, ignore=shutil.ignore_patterns("__pycache__"))
                tg_names.append(d.name)
                print(f"  + tools: {d.name}")

    # Save manifest with activation state
    manifest = {
        "name": args.name,
        "description": args.description,
        "workflows": wf_names,
        "tool_groups": tg_names,
        "active_tools": cfg.get("active_tools", []),
        "active_workflows": cfg.get("active_workflows", []),
    }
    (preset_dir / "preset.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nPreset saved: {preset_dir}")
    print(f"  {len(wf_names)} workflow(s), {len(tg_names)} tool group(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
