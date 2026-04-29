#!/usr/bin/env python3
"""List issues from the Google Sheets project tracker.

Inputs:
  --state: str — Filter by state ("open", "closed", or "all"; default "open").
  --limit: int — Max rows to return (default 30).
  --label: str (repeatable) — Filter by one or more labels.

Reads the "issues" tab and prints matching rows as a formatted table."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import ISSUES_HEADERS, ISSUES_TAB, read_all_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="List issues from Google Sheet")
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--label", action="append", default=[])
    args = parser.parse_args()

    try:
        rows = read_all_rows(ISSUES_TAB)
    except Exception as exc:
        print(f"Error reading sheet: {exc}", file=sys.stderr)
        return 1

    if len(rows) <= 1:
        print("No issues found.")
        return 0

    header = rows[0]
    data = rows[1:]

    # Resolve column indices from header (fall back to defaults)
    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return ISSUES_HEADERS.index(name)

    id_col = col("ID")
    state_col = col("State")
    labels_col = col("Labels")
    title_col = col("Title")
    created_col = col("Created")

    def _get(row, idx, default=""):
        return row[idx] if idx < len(row) else default

    # Filter
    filtered = []
    for row in data:
        if args.state != "all":
            row_state = _get(row, state_col).lower()
            if row_state != args.state.lower():
                continue
        if args.label:
            row_labels = [l.strip().lower() for l in _get(row, labels_col).split(",") if l.strip()]
            if not all(lbl.lower() in row_labels for lbl in args.label):
                continue
        filtered.append(row)

    filtered = filtered[: args.limit]

    if not filtered:
        print(f"No {args.state} issues found.")
        return 0

    # Print table
    print(f"{'ID':<6} {'State':<10} {'Title':<50} {'Labels':<20} {'Created'}")
    print("-" * 100)
    for row in filtered:
        print(
            f"{_get(row, id_col):<6} "
            f"{_get(row, state_col):<10} "
            f"{_get(row, title_col):<50} "
            f"{_get(row, labels_col):<20} "
            f"{_get(row, created_col)}"
        )

    print(f"\n{len(filtered)} issue(s) shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
