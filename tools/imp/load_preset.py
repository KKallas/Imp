#!/usr/bin/env python3
"""Load a preset — install missing workflows/tools and activate them.

Inputs:
  --name: str — preset name to load.
  --force: overwrite existing workflows/tools.

Process: Copies missing workflows and tools from the preset into the
         project, then sets the activation state from the preset manifest.
Output: Prints what was installed and activated."""
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


def _save_config(cfg: dict) -> None:
    cfg_file = ROOT / ".imp" / "config.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(cfg, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Load a preset")
    parser.add_argument("--name", required=True, help="Preset name")
    parser.add_argument("--force", action="store_true", help="Overwrite existing")
    args = parser.parse_args()

    preset_dir = PRESETS_DIR / args.name
    if not preset_dir.is_dir():
        print(f"Preset '{args.name}' not found.")
        return 1

    manifest = {}
    mf = preset_dir / "preset.json"
    if mf.exists():
        manifest = json.loads(mf.read_text())
        print(f"Loading preset: {manifest.get('name', args.name)}")
        if manifest.get("description"):
            print(f"  {manifest['description']}")

    # Install workflows (skip existing unless --force)
    wf_src = preset_dir / "workflows"
    if wf_src.is_dir():
        for wf in sorted(wf_src.iterdir()):
            if not wf.is_dir():
                continue
            dst = ROOT / "workflows" / wf.name
            if dst.exists() and not args.force:
                print(f"  = workflow exists: {wf.name}")
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(wf, dst)
            print(f"  + installed workflow: {wf.name}")

    # Install tools (skip existing unless --force)
    tg_src = preset_dir / "tools"
    if tg_src.is_dir():
        for tg in sorted(tg_src.iterdir()):
            if not tg.is_dir():
                continue
            dst = ROOT / "tools" / tg.name
            if dst.exists() and not args.force:
                print(f"  = tools exist: {tg.name}")
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(tg, dst)
            print(f"  + installed tools: {tg.name}")

    # Set activation state from preset
    cfg = _load_config()
    if manifest.get("active_tools"):
        cfg["active_tools"] = manifest["active_tools"]
        print(f"  Active tools: {', '.join(manifest['active_tools'])}")
    if manifest.get("active_workflows"):
        cfg["active_workflows"] = manifest["active_workflows"]
        print(f"  Active workflows: {', '.join(manifest['active_workflows'])}")
    _save_config(cfg)

    print("\nPreset loaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
