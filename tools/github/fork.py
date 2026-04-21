#!/usr/bin/env python3
"""Fork a GitHub repository without cloning it locally.

Args:
    owner/repo (str): The GitHub repository identifier to fork (e.g. "octocat/Hello-World").

Process:
    Invokes `gh repo fork` via subprocess with `--clone=false` to create a remote fork.

Output:
    Prints gh stdout on success, stderr on failure. Returns the gh exit code."""
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
