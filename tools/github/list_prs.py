#!/usr/bin/env python3
"""List GitHub pull requests by invoking the `gh pr list` command.

Inputs:
  --state: str — Filter by PR state: "open", "closed", "merged", or "all" (default: "open").
  --limit: int — Maximum number of PRs to return (default: 30).
  --repo: str | None — Target repository in OWNER/REPO format; defaults to the current repo.

Process: Builds and runs a `gh pr list` subprocess with the given filters.
Output: Prints PR listing to stdout, errors to stderr; returns the process exit code."""

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="List pull requests")
    parser.add_argument("--state", default="open", choices=["open", "closed", "merged", "all"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    cmd = ["gh", "pr", "list", "--state", args.state, "--limit", str(args.limit)]
    if args.repo:
        cmd.extend(["--repo", args.repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
