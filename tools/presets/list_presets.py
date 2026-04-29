#!/usr/bin/env python3
"""List all saved presets.

Inputs: None.

Process: Scans .imp/presets/ for preset folders with manifest files.
Output: Prints preset names, descriptions, and contents."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


def main() -> int:
    if not PRESETS_DIR.is_dir():
        print("No presets saved yet.")
        return 0

    presets = sorted(
        d for d in PRESETS_DIR.iterdir()
        if d.is_dir() and (d / "preset.json").exists()
    )

    if not presets:
        print("No presets saved yet.")
        return 0

    for p in presets:
        manifest = json.loads((p / "preset.json").read_text())
        name = manifest.get("name", p.name)
        desc = manifest.get("description", "")
        wf = manifest.get("workflow_count", 0)
        tg = manifest.get("tool_group_count", 0)
        print(f"  {name}  ({wf} workflows, {tg} tool groups)")
        if desc:
            print(f"    {desc}")

    print(f"\n{len(presets)} preset(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
