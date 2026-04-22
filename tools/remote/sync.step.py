"""Start developer sync session."""

import socket
import subprocess


def run(context):
    # Find LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "127.0.0.1"

    server_url = f"http://{ip}:8421"
    download_url = f"{server_url}/imp-sync.py"

    result = subprocess.run(
        ["python", "tools/remote/sync.py"],
        capture_output=True, text=True,
    )

    return {
        "ok": True,
        "output": result.stdout.strip() or f"Sync ready at {server_url}",
        "server_url": server_url,
        "download_url": download_url,
    }
