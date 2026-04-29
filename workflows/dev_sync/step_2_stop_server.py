"""End the sync session — remove session marker to disable sync access."""

import json
from pathlib import Path

SESSION_FILE = Path(".imp/remote_session.json")


def run(context):
    if not SESSION_FILE.exists():
        return {
            "ok": True,
            "output": "No active session to stop.",
        }

    # Read session info for the output message
    try:
        session = json.loads(SESSION_FILE.read_text())
        ip = session.get("ip", "unknown")
        port = session.get("port", "unknown")
        detail = f" ({ip}:{port})"
    except Exception:
        detail = ""

    # Remove marker — server will stop gating sync endpoints
    SESSION_FILE.unlink(missing_ok=True)

    return {
        "ok": True,
        "output": f"Sync session stopped{detail}. Endpoints no longer accessible.",
    }
