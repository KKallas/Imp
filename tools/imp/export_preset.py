#!/usr/bin/env python3
"""Export a preset as a zip file for sharing.

Inputs:
  --name: str — preset name to export.
  --output: str — output zip path (default: <name>.zip in current dir).

Process: Zips the preset folder.
Output: Prints the output path."""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a preset as zip")
    parser.add_argument("--name", required=True, help="Preset name")
    parser.add_argument("--output", default="", help="Output zip path")
    args = parser.parse_args()

    preset_dir = PRESETS_DIR / args.name
    if not preset_dir.is_dir():
        print(f"Preset '{args.name}' not found.")
        return 1

    out = args.output or f"{args.name}.zip"
    if not out.endswith(".zip"):
        out += ".zip"

    shutil.make_archive(out.replace(".zip", ""), "zip", preset_dir)
    print(f"Exported: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
