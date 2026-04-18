#!/usr/bin/env python3
"""Pull latest changes from remote."""

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
