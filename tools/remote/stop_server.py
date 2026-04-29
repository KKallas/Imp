#!/usr/bin/env python3
"""Stop a developer remote session.

Inputs: None.

Process: Removes the session marker file (.imp/remote_session.json),
which tells the server that sync access is no longer active.
Does NOT stop the server itself.

Output: Prints whether a session was stopped or none was active."""

import json
import sys
from pathlib import Path

SESSION_FILE = Path(".imp/remote_session.json")


def main() -> int:
    if not SESSION_FILE.exists():
        print("No active remote session.")
        return 0

    try:
        session = json.loads(SESSION_FILE.read_text())
        started = session.get("started", "unknown")
        ip = session.get("ip", "unknown")
        port = session.get("port", "unknown")
        print(f"Stopping session (started {started}, {ip}:{port})...")
    except Exception:
        print("Stopping session...")

    SESSION_FILE.unlink(missing_ok=True)
    print("Remote session stopped. Sync endpoints are no longer accessible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
