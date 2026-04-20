"""Run heuristics to infer durations and dependencies."""

import subprocess


def run(context):
    result = subprocess.run(
        ["python", "pipeline/heuristics.py"],
        capture_output=True, text=True,
    )
    return {
        "ok": result.returncode == 0,
        "output": result.stdout[:2000],
    }
