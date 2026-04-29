#!/usr/bin/env python3
"""Close an issue in the Google Sheets project tracker.

Inputs:
  issue (int): Issue ID to close.
  --reason (str): "completed" (default) or "not_planned".
  --comment (str): Optional comment appended to the Body cell.

Updates the State column and Updated timestamp for the matching row."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import (
    ISSUES_HEADERS,
    ISSUES_TAB,
    _now_iso,
    find_row_by_id,
    read_all_rows,
    update_cell,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Close an issue in Google Sheet")
    parser.add_argument("issue", type=int)
    parser.add_argument("--reason", default="completed", choices=["completed", "not_planned"])
    parser.add_argument("--comment", default=None)
    args = parser.parse_args()

    try:
        row_idx = find_row_by_id(ISSUES_TAB, args.issue)
        if row_idx is None:
            print(f"Issue #{args.issue} not found.", file=sys.stderr)
            return 1

        rows = read_all_rows(ISSUES_TAB)
        header = rows[0]

        def col(name: str) -> int:
            try:
                return header.index(name)
            except ValueError:
                return ISSUES_HEADERS.index(name)

        state_col = col("State")
        body_col = col("Body")
        updated_col = col("Updated")

        # Update state
        state_value = f"closed ({args.reason})"
        update_cell(ISSUES_TAB, row_idx, state_col, state_value)

        # Append comment to body if provided
        if args.comment:
            existing_body = rows[row_idx][body_col] if body_col < len(rows[row_idx]) else ""
            new_body = f"{existing_body}\n\n---\n{args.comment}" if existing_body else args.comment
            update_cell(ISSUES_TAB, row_idx, body_col, new_body)

        # Update timestamp
        update_cell(ISSUES_TAB, row_idx, updated_col, _now_iso())

        print(f"Closed issue #{args.issue} as {args.reason}.")
        if args.comment:
            print(f"  Comment added: {args.comment}")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
