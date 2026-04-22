"""Start developer sync session."""

import socket


def _get_server_url():
    """Get the server URL using the machine's IP."""
    try:
        # Connect to an external address to find our LAN IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"
    return f"http://{ip}:8421"


def run(context):
    server_url = _get_server_url()
    download_url = f"{server_url}/imp-sync.py"

    return {
        "ok": True,
        "pause": True,
        "title": "Developer sync active",
        "detail_html": f"""
            <h3>Developer Sync Active</h3>
            <p>The sync endpoint is ready. Download the sync script and run it locally:</p>
            <p style="margin:16px 0;">
                <a href="{download_url}" download="imp-sync.py"
                   style="display:inline-block;padding:8px 20px;background:#58a6ff;color:#fff;
                          border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">
                    Download imp-sync.py
                </a>
            </p>
            <p style="font-size:13px;color:#8b949e;">
                Or copy this command:<br>
                <code style="background:#161b22;padding:4px 8px;border-radius:4px;font-size:12px;">
                    curl -o imp-sync.py {download_url} && python imp-sync.py
                </code>
            </p>
            <p style="font-size:12px;color:#8b949e;margin-top:12px;">
                Server: {server_url}<br>
                Syncing: tools/, workflows/, renderers/, public/
            </p>
        """,
        "actions": [
            {"label": "Stop sync", "action": "continue"},
        ],
        "output": f"Sync endpoint active at {server_url}. Download: {download_url}",
    }
