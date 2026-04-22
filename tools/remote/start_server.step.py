"""Start the Imp server and pause with sync download link."""

import socket
import subprocess


def _get_server_url():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return f"http://{ip}:8421"


def run(context):
    # Start the server if not running
    result = subprocess.run(
        ["python", "tools/remote/start_server.py"],
        capture_output=True, text=True,
    )

    server_url = _get_server_url()
    download_url = f"{server_url}/imp-sync.py"

    return {
        "pause": True,
        "title": "Developer sync active",
        "detail_html": (
            "<h3>Developer Sync Active</h3>"
            "<p>The sync endpoint is ready. Download the sync script and run it locally:</p>"
            f'<p style="margin:16px 0;">'
            f'<a href="{download_url}" download="imp-sync.py"'
            f' style="display:inline-block;padding:8px 20px;background:#58a6ff;color:#fff;'
            f' border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">'
            f"Download imp-sync.py</a></p>"
            f'<p style="font-size:13px;color:#8b949e;">'
            f"Or copy this command:<br>"
            f'<code style="background:#161b22;padding:4px 8px;border-radius:4px;font-size:12px;">'
            f"curl -o imp-sync.py {download_url} && python imp-sync.py</code></p>"
            f'<p style="font-size:12px;color:#8b949e;margin-top:12px;">'
            f"Server: {server_url}<br>"
            f"Syncing: tools/, workflows/, renderers/, public/</p>"
        ),
        "actions": [
            {"label": "Stop sync", "action": "continue"},
        ],
        "ok": True,
        "output": result.stdout.strip() or f"Server at {server_url}",
        "server_url": server_url,
        "download_url": download_url,
    }
