"""Workflow step that pushes local git commits to a remote repository."""
import subprocess


def run(context):
    previous_results = context.get("previous_results", {})
    branch = previous_results.get("branch")

    cmd = ["python", "tools/github/push.py"]
    if branch:
        cmd.append(branch)

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout
    if result.stderr:
        output += "\n" + result.stderr

    return {
        "ok": result.returncode == 0,
        "output": output.strip(),
        "exit_code": result.returncode,
    }
