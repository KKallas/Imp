#!/usr/bin/env python3
"""Merge a pull request."""

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge a pull request")
    parser.add_argument("pr", type=int)
    parser.add_argument("--method", default="squash", choices=["merge", "squash", "rebase"])
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    cmd = ["gh", "pr", "merge", str(args.pr), f"--{args.method}"]
    if args.repo:
        cmd.extend(["--repo", args.repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
