"""Run heuristics to infer durations and dependencies."""

import subprocess


def run(context):
    result = subprocess.run(
        ["python", "pipeline/heuristics.py"],
        capture_output=True, text=True,
    )
    summary = result.stderr.strip().split("\n")[-1] if result.stderr.strip() else "Heuristics complete"
    return {
        "ok": result.returncode == 0,
        "output": summary,
    }
