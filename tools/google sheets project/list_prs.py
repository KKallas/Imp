#!/usr/bin/env python3
"""List pull requests from the Google Sheets project tracker.

Inputs:
  --state: str — Filter by PR state ("open", "closed", "merged", or "all"; default "open").
  --limit: int — Max rows to return (default 30).

Reads the "pull_requests" tab and prints matching rows as a formatted table."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import PRS_HEADERS, PRS_TAB, read_all_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="List PRs from Google Sheet")
    parser.add_argument("--state", default="open", choices=["open", "closed", "merged", "all"])
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    try:
        rows = read_all_rows(PRS_TAB)
    except Exception as exc:
        print(f"Error reading sheet: {exc}", file=sys.stderr)
        return 1

    if len(rows) <= 1:
        print("No pull requests found.")
        return 0

    header = rows[0]
    data = rows[1:]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return PRS_HEADERS.index(name)

    id_col = col("ID")
    state_col = col("State")
    title_col = col("Title")
    base_col = col("Base")
    head_col = col("Head")
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
        filtered.append(row)

    filtered = filtered[: args.limit]

    if not filtered:
        print(f"No {args.state} pull requests found.")
        return 0

    # Print table
    print(f"{'ID':<6} {'State':<10} {'Title':<40} {'Base':<15} {'Head':<15} {'Created'}")
    print("-" * 100)
    for row in filtered:
        print(
            f"{_get(row, id_col):<6} "
            f"{_get(row, state_col):<10} "
            f"{_get(row, title_col):<40} "
            f"{_get(row, base_col):<15} "
            f"{_get(row, head_col):<15} "
            f"{_get(row, created_col)}"
        )

    print(f"\n{len(filtered)} pull request(s) shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
