#!/usr/bin/env python3
"""Start a bidirectional file sync session between the server and a developer's local machine.

Run this tool to activate the sync endpoint. Then download imp-sync.py from
the queue popup or from http://<server-ip>:8421/imp-sync.py and run it locally.

Inputs:
  (none) — the sync endpoint is always available while the server runs.

Process: Prints the download URL for the sync client script. The sync API
endpoints (manifest, file upload/download) are built into the server.

Output: Prints the download URL and server IP."""

import socket
import sys


def main() -> int:
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

    print("=" * 50)
    print("  Imp Developer Sync")
    print("=" * 50)
    print()
    print(f"Server: {server_url}")
    print(f"Download sync client: {download_url}")
    print()
    print("On your local machine run:")
    print(f"  curl -o imp-sync.py {download_url}")
    print(f"  python imp-sync.py")
    print()
    print("The sync API is always active while the server runs.")
    print("No need to start/stop — just run the client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
