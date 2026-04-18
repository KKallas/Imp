#!/usr/bin/env python3
"""Close a GitHub issue."""

import argparse
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Close a GitHub issue")
    parser.add_argument("issue", type=int)
    parser.add_argument("--reason", default="completed", choices=["completed", "not_planned"])
    parser.add_argument("--comment", default=None)
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    if args.comment:
        comment_cmd = ["gh", "issue", "comment", str(args.issue), "--body", args.comment]
        if args.repo:
            comment_cmd.extend(["--repo", args.repo])
        subprocess.run(comment_cmd, capture_output=True, text=True)

    cmd = ["gh", "issue", "close", str(args.issue), "--reason", args.reason]
    if args.repo:
        cmd.extend(["--repo", args.repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
