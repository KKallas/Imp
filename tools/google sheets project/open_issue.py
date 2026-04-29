#!/usr/bin/env python3
"""Create a new issue in the Google Sheets project tracker.

Inputs:
  --title (str, required): Issue title.
  --body (str): Issue body text (default: empty).
  --label (str, repeatable): Labels to apply.

Appends a new row to the "issues" tab with state "open" and prints the new ID."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import ISSUES_TAB, _now_iso, append_row, next_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an issue in Google Sheet")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--label", action="append", default=[])
    args = parser.parse_args()

    try:
        issue_id = next_id(ISSUES_TAB)
        now = _now_iso()
        labels_str = ", ".join(args.label)

        row = [
            str(issue_id),    # ID
            args.title,       # Title
            args.body,        # Body
            "open",           # State
            labels_str,       # Labels
            now,              # Created
            now,              # Updated
        ]
        append_row(ISSUES_TAB, row)

        print(f"Created issue #{issue_id}: {args.title}")
        if labels_str:
            print(f"  Labels: {labels_str}")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
