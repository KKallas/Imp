#!/usr/bin/env python3
"""Pull latest changes from a Git remote, optionally targeting a specific branch.

Args:
    branch (str, optional): Branch name passed as the first CLI argument. If omitted, pulls the current tracking branch.

Runs ``git pull [origin <branch>]`` as a subprocess, printing stdout and stderr.

Returns:
    int: The git process exit code (0 on success, non-zero on failure)."""
import subprocess
import sys


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else None
    cmd = ["git", "pull"]
    if branch:
        cmd.extend(["origin", branch])
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
