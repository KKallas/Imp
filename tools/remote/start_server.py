#!/usr/bin/env python3
"""Start the Imp render server on port 8421.

Inputs:
  --port (int, optional): Port to run on (default: 8421).

Process: Checks if the server is already running. If not, spawns it as a
background process. Waits for the health endpoint to respond.

Output: Prints the server URL or reports that it's already running."""

import argparse
import socket
import subprocess
import sys
import time
import urllib.request


def is_running(port):
    """Check if the server is already responding."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the Imp server")
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()

    if is_running(args.port):
        ip = get_lan_ip()
        print(f"Server already running at http://{ip}:{args.port}")
        return 0

    # Check if port is in use by something else
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", args.port))
        s.close()
        print(f"Port {args.port} is in use but not responding to /health")
        print("Another process may be using this port. Check with: lsof -i :{args.port}")
        return 1
    except (ConnectionRefusedError, OSError):
        pass  # port is free

    print(f"Starting server on port {args.port}...")
    log_file = f"/tmp/imp-server-{args.port}.log"
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.render_route", "--port", str(args.port)],
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )

    # Wait for it to come up
    for i in range(10):
        time.sleep(1)
        # Check if process died
        if proc.poll() is not None:
            log_fh.close()
            try:
                log_content = open(log_file).read().strip()
            except Exception:
                log_content = ""
            print(f"Server process exited with code {proc.returncode}")
            if log_content:
                print(f"Log output:\n{log_content[-1000:]}")
            return 1
        if is_running(args.port):
            log_fh.close()
            ip = get_lan_ip()
            print(f"Server started at http://{ip}:{args.port}")
            print(f"Log: {log_file}")
            return 0

    log_fh.close()
    try:
        log_content = open(log_file).read().strip()
    except Exception:
        log_content = ""
    print(f"Server failed to respond within 10 seconds (PID {proc.pid})")
    if log_content:
        print(f"Log output:\n{log_content[-1000:]}")
    else:
        print(f"No log output. Check: {log_file}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
