import subprocess


def run(context):
    previous_results = context.get("previous_results", {})
    branch = previous_results.get("branch")
    cmd = ["python", "tools/github/pull.py"]
    if branch:
        cmd.append(branch)
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip()
    if result.stderr:
        output += "\n" + result.stderr.strip()
    return {
        "ok": result.returncode == 0,
        "output": output,
        "exit_code": result.returncode,
    }
