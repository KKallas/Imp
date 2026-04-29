#!/usr/bin/env python3
"""Create or connect to the Google Sheets project tracker.

Inputs:
  --id: str — Manually provide an existing spreadsheet ID to connect to.
  --force-new: flag — Force creation of a new spreadsheet even if one exists.

Process:
  1. If --id is given, validate it and store it in config.
  2. Otherwise look up the stored config, search Drive, or create a new sheet.
  The spreadsheet will have two tabs: "issues" and "pull_requests",
  each pre-populated with column headers.

Output: Prints the spreadsheet ID and URL."""

import argparse
import sys
from pathlib import Path

# Allow importing sibling module
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sheets import (
    SHEET_TITLE,
    create_spreadsheet,
    ensure_spreadsheet,
    find_spreadsheet,
    get_spreadsheet_id,
    set_spreadsheet_id,
    sheets_service,
)


def _url(sid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sid}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or connect to the project tracker spreadsheet",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Existing spreadsheet ID to connect to",
    )
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="Create a new spreadsheet even if one already exists",
    )
    args = parser.parse_args()

    try:
        if args.id:
            # Validate provided ID
            svc = sheets_service()
            props = svc.spreadsheets().get(
                spreadsheetId=args.id, fields="properties.title",
            ).execute()
            title = props["properties"]["title"]
            set_spreadsheet_id(args.id)
            print(f"Connected to existing spreadsheet: {title}")
            print(f"  ID:  {args.id}")
            print(f"  URL: {_url(args.id)}")
            return 0

        if args.force_new:
            sid = create_spreadsheet()
            set_spreadsheet_id(sid)
            print(f"Created new spreadsheet: {SHEET_TITLE}")
            print(f"  ID:  {sid}")
            print(f"  URL: {_url(sid)}")
            return 0

        # Default: find or create
        sid = ensure_spreadsheet()
        # Determine if it was freshly created or existing
        existing = get_spreadsheet_id()
        print(f"Spreadsheet ready: {SHEET_TITLE}")
        print(f"  ID:  {sid}")
        print(f"  URL: {_url(sid)}")
        return 0

    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
