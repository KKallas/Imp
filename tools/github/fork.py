#!/usr/bin/env python3
"""Fork a GitHub repository."""

import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: fork.py <owner/repo>", file=sys.stderr)
        return 1
    repo = sys.argv[1]
    result = subprocess.run(
        ["gh", "repo", "fork", repo, "--clone=false"],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
