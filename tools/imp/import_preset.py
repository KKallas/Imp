#!/usr/bin/env python3
"""Import a preset from a zip file.

Inputs:
  path (positional): Path to the zip file.

Process: Extracts the zip into .imp/presets/<name>/.
Output: Prints the imported preset name."""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRESETS_DIR = ROOT / ".imp" / "presets"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: import_preset.py <path-to-zip>", file=sys.stderr)
        return 1

    zip_path = Path(sys.argv[1])
    if not zip_path.exists():
        print(f"File not found: {zip_path}")
        return 1

    PRESETS_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        # Try to find preset.json to get the name
        manifest_name = None
        for name in zf.namelist():
            if name.endswith("preset.json"):
                data = json.loads(zf.read(name))
                manifest_name = data.get("name")
                break

        preset_name = manifest_name or zip_path.stem
        dest = PRESETS_DIR / preset_name

        if dest.exists():
            print(f"Preset '{preset_name}' already exists. Remove it first.")
            return 1

        dest.mkdir()
        zf.extractall(dest)

    print(f"Imported preset: {preset_name}")
    print(f"  Location: {dest}")
    print(f"  Run: python tools/presets/load_preset.py --name {preset_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
