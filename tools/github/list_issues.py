#!/usr/bin/env python3
"""List GitHub issues."""

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
