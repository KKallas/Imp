"""Activate developer sync — create session marker, pause with download link."""

import json
import socket
import time
from pathlib import Path

SESSION_FILE = Path(".imp/remote_session.json")
DEFAULT_PORT = 8421


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run(context):
    port = DEFAULT_PORT
    ip = get_lan_ip()
    url = f"http://{ip}:{port}"

    # Create session marker — this tells the server sync is active
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    session = {
        "active": True,
        "port": port,
        "ip": ip,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))

    # Pause and show the download link
    return {
        "pause": True,
        "title": "Developer sync active",
        "detail_html": (
            "<h3>Developer Sync Active</h3>"
            "<p>Sync endpoints are now accessible. Download the sync script and run it on your local machine:</p>"
            '<p style="margin:16px 0;">'
            f'<a href="/imp-sync.py" download="imp-sync.py"'
            ' style="display:inline-block;padding:8px 20px;background:#58a6ff;color:#fff;'
            ' border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">'
            "Download imp-sync.py</a></p>"
            '<p style="font-size:13px;color:#8b949e;">'
            "Or copy this command:<br>"
            '<code id="sync-cmd" style="background:#161b22;padding:4px 8px;border-radius:4px;font-size:12px;">'
            f"curl -o imp-sync.py {url}/imp-sync.py && python imp-sync.py</code></p>"
            '<p style="font-size:12px;color:#8b949e;margin-top:12px;">'
            "Syncing: tools/, workflows/, renderers/, public/</p>"
        ),
        "actions": [
            {"label": "Stop sync", "action": "continue"},
        ],
        "ok": True,
        "output": f"Sync active at {url} — download link in queue popup",
    }
