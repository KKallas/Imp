#!/usr/bin/env python3
"""Push local commits to a remote repository via git push.

Inputs:
branch (str, optional): Target branch name; omitted means push the current branch to its default remote.

Process: Builds a git push command (appending origin <branch> if provided) and runs it as a subprocess.
Output: Prints git stdout/stderr and returns the process exit code."""
import subprocess
import sys


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else None
    cmd = ["git", "push"]
    if branch:
        cmd.extend(["origin", branch])
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
