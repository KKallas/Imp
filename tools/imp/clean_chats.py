#!/usr/bin/env python3
"""Delete chat history files and optionally execution logs.

Inputs:
  --include-logs: Also delete files in public/logs/ (default: off).
  --dry-run: List files that would be deleted without removing them.

Process: Scans .imp/chats/ (and optionally public/logs/) for files,
         deletes them, and reports a summary.
Output: Prints the count and paths of deleted (or to-be-deleted) files."""
import argparse
import os
import sys
from pathlib import Path

CHAT_DIR = Path(".imp/chats")
LOGS_DIR = Path("public/logs")


def _collect_files(directory: Path) -> list[Path]:
    """Return all files in *directory* (non-recursive)."""
    if not directory.is_dir():
        return []
    return sorted(f for f in directory.iterdir() if f.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up Imp chat history and logs")
    parser.add_argument(
        "--include-logs",
        action="store_true",
        help="Also delete execution logs in public/logs/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    targets: dict[str, list[Path]] = {"chats": _collect_files(CHAT_DIR)}
    if args.include_logs:
        targets["logs"] = _collect_files(LOGS_DIR)

    total = 0
    for label, files in targets.items():
        if not files:
            print(f"{label}: nothing to clean")
            continue

        action = "would delete" if args.dry_run else "deleted"
        for f in files:
            if not args.dry_run:
                f.unlink()
            print(f"  {action}: {f}")
        print(f"{label}: {action} {len(files)} file(s)")
        total += len(files)

    if total == 0:
        print("Nothing to clean up.")
    else:
        verb = "Would delete" if args.dry_run else "Deleted"
        print(f"\n{verb} {total} file(s) total.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
