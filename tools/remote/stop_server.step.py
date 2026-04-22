"""Stop the Imp server."""

import subprocess


def run(context):
    result = subprocess.run(
        ["python", "tools/remote/stop_server.py"],
        capture_output=True, text=True,
    )
    output = result.stdout.strip() or result.stderr.strip()
    return {
        "ok": result.returncode == 0,
        "output": output,
    }
