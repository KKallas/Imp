#!/usr/bin/env python3
"""Close a GitHub issue via the `gh` CLI, optionally adding a comment first.

Inputs:
  issue (int): Issue number to close.
  --reason (str): Close reason, "completed" (default) or "not_planned".
  --comment (str): Optional comment to post before closing.
  --repo (str): Optional "owner/repo" target; defaults to the current repo.

Process: Posts the comment (if given) then closes the issue using `gh issue close`.
Output: Prints gh stdout/stderr and returns the process exit code."""
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
