#!/usr/bin/env python3
"""List GitHub issues via the `gh` CLI with optional filters.

Inputs:
  --state: str — filter by issue state ("open", "closed", or "all"; default "open").
  --limit: int — max number of issues to return (default 30).
  --label: str (repeatable) — filter by one or more labels.
  --repo: str — target a specific "owner/repo" instead of the current repository.

Builds and runs a `gh issue list` subprocess, prints stdout and stderr, and returns the process exit code."""
import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="List GitHub issues")
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    cmd = ["gh", "issue", "list", "--state", args.state, "--limit", str(args.limit)]
    for label in args.label:
        cmd.extend(["--label", label])
    if args.repo:
        cmd.extend(["--repo", args.repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
