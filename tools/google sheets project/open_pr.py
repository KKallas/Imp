#!/usr/bin/env python3
"""Create a pull request entry in the Google Sheets project tracker.

Inputs:
  --title (str, required): PR title.
  --body (str): PR description (default: empty).
  --base (str): Target branch (default: "main").
  --head (str): Source branch (default: empty).

Appends a new row to the "pull_requests" tab with state "open"."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import PRS_TAB, _now_iso, append_row, next_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a PR in Google Sheet")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--base", default="main")
    parser.add_argument("--head", default="")
    args = parser.parse_args()

    try:
        pr_id = next_id(PRS_TAB)
        now = _now_iso()

        row = [
            str(pr_id),       # ID
            args.title,       # Title
            args.body,        # Body
            "open",           # State
            args.base,        # Base
            args.head,        # Head
            now,              # Created
            now,              # Updated
        ]
        append_row(PRS_TAB, row)

        print(f"Created PR #{pr_id}: {args.title}")
        print(f"  Base: {args.base}")
        if args.head:
            print(f"  Head: {args.head}")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
