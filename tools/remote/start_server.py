#!/usr/bin/env python3
"""Start a developer remote session.

Inputs:
  --port (int, optional): Server port (default: 8421).

Process: Verifies the server is running and the sync endpoints
(/health, /imp-sync.py, /api/sync/manifest) are all accessible.
If everything checks out, writes a session marker file to
.imp/remote_session.json so the server knows sync is active.

Output: Prints connection details or which endpoints failed."""

import argparse
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

SESSION_FILE = Path(".imp/remote_session.json")


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check_endpoint(url, label):
    """Return True if the endpoint responds with 2xx."""
    try:
        urllib.request.urlopen(url, timeout=5)
        print(f"  OK  {label}")
        return True
    except Exception as e:
        print(f"  FAIL  {label}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start a developer remote session",
    )
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    ip = get_lan_ip()
    base = f"http://127.0.0.1:{args.port}"
    url = f"http://{ip}:{args.port}"

    # ── verify every endpoint the session needs ──────────────────
    print("Checking endpoints...")
    ok = True
    ok &= check_endpoint(f"{base}/health", "/health")
    ok &= check_endpoint(f"{base}/imp-sync.py", "/imp-sync.py")
    ok &= check_endpoint(f"{base}/api/sync/manifest", "/api/sync/manifest")

    if not ok:
        print("\nOne or more endpoints are not accessible.")
        print(f"Make sure the server is running: python -m server.render_route --port {args.port}")
        return 1

    # ── activate session ─────────────────────────────────────────
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    session = {
        "active": True,
        "port": args.port,
        "ip": ip,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))

    print(f"\nRemote session active at {url}")
    print(f"Sync script:  {url}/imp-sync.py")
    print(f"Run on client: curl -o imp-sync.py {url}/imp-sync.py && python imp-sync.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
