#!/usr/bin/env python3
"""Open a GitHub pull request via the gh CLI.

Inputs:
--title (str): PR title (required).
--body (str): PR description; defaults to empty.
--base (str): Target branch; omitted if unset.
--head (str): Source branch; omitted if unset.
--repo (str): Optional "owner/repo" target; defaults to the current repo.

Process: Builds and runs gh pr create with the provided arguments.
Output: Prints gh stdout/stderr and returns the process exit code."""
import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a pull request")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--base", default=None)
    parser.add_argument("--head", default=None)
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    cmd = ["gh", "pr", "create", "--title", args.title, "--body", args.body]
    if args.base:
        cmd.extend(["--base", args.base])
    if args.head:
        cmd.extend(["--head", args.head])
    if args.repo:
        cmd.extend(["--repo", args.repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
