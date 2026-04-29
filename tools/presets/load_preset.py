#!/usr/bin/env python3
"""Load a preset — copy its workflows and tools into the project.

Inputs:
  --name: str — preset name to load.
  --force: overwrite existing workflows/tools if they conflict.

Process: Copies workflows and tools from .imp/presets/<name>/ into
         the project's workflows/ and tools/ directories.
Output: Prints what was installed."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


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

    # Copy workflows
    wf_src = preset_dir / "workflows"
    if wf_src.is_dir():
        for wf in sorted(wf_src.iterdir()):
            if not wf.is_dir():
                continue
            dst = ROOT / "workflows" / wf.name
            if dst.exists() and not args.force:
                print(f"  ! workflow exists (skip): {wf.name} (use --force)")
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(wf, dst)
            print(f"  + workflow: {wf.name}")

    # Copy tools
    tg_src = preset_dir / "tools"
    if tg_src.is_dir():
        for tg in sorted(tg_src.iterdir()):
            if not tg.is_dir():
                continue
            dst = ROOT / "tools" / tg.name
            if dst.exists() and not args.force:
                print(f"  ! tool group exists (skip): {tg.name} (use --force)")
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(tg, dst)
            print(f"  + tools: {tg.name}")

    print("\nPreset loaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
