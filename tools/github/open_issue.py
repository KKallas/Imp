#!/usr/bin/env python3
"""Open a new GitHub issue."""

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a GitHub issue")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    cmd = ["gh", "issue", "create", "--title", args.title, "--body", args.body]
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
