"""Clean up Imp chat history and optionally execution logs."""

import subprocess


def run(context):
    cmd = ["python", "tools/imp/clean_chats.py", "--include-logs"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    return {
        "ok": result.returncode == 0,
        "output": result.stdout.strip() or result.stderr.strip(),
    }
