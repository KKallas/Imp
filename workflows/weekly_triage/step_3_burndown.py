"""Generate burndown chart."""

import subprocess


def run(context):
    result = subprocess.run(
        ["python", "-m", "renderers.helpers", "--template", "burndown"],
        capture_output=True, text=True,
    )
    chart_path = result.stdout.strip()
    return {
        "ok": result.returncode == 0,
        "output": chart_path,
    }
