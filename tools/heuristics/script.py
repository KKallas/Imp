#!/usr/bin/env python3
"""tools/heuristics/script.py — run calibration + enrichment pipeline.

Thin wrapper that calls calibrate then the existing heuristics pipeline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    from tools.heuristics.calibrate import calibrate_from_enriched

    # Run calibration (updates .imp/calibration.json)
    cal = calibrate_from_enriched()
    print(json.dumps(cal, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
