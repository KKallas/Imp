#!/usr/bin/env python3
"""Imp — entry point.

Starts the Imp web service. The terminal does only:

  1. Check Python version
  2. Verify required Python packages are installed; offer to install if missing
  3. Start uvicorn against `server.app:app`
  4. Print the URL

After this, the terminal only shows logs. All further configuration happens
in the browser via the Setup Agent.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REQUIRED_PYTHON = (3, 11)
ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = ROOT / "requirements.txt"
STATE_DIR = ROOT / ".imp"
HOST = "127.0.0.1"
PORT = 8420

# Pip package name → import name (only when they differ)
IMPORT_NAME_OVERRIDES = {
    "claude-agent-sdk": "claude_agent_sdk",
    "argon2-cffi": "argon2",
}


def check_python_version() -> None:
    if sys.version_info < REQUIRED_PYTHON:
        major, minor = REQUIRED_PYTHON
        have = ".".join(str(p) for p in sys.version_info[:3])
        print(f"Imp requires Python {major}.{minor}+. You have {have}.")
        sys.exit(1)


def read_requirements() -> list[str]:
    """Return pip package names from requirements.txt, version specifiers stripped."""
    if not REQUIREMENTS_FILE.exists():
        print(f"Missing {REQUIREMENTS_FILE.name}. Cannot determine required packages.")
        sys.exit(1)
    packages: list[str] = []
    for raw in REQUIREMENTS_FILE.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break
        packages.append(line)
    return packages


def find_missing(packages: list[str]) -> list[str]:
    missing: list[str] = []
    for pkg in packages:
        import_name = IMPORT_NAME_OVERRIDES.get(pkg, pkg.replace("-", "_"))
        if importlib.util.find_spec(import_name) is None:
            missing.append(pkg)
    return missing


def install(packages: list[str]) -> None:
    print(f"\nInstalling: {', '.join(packages)}\n")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *packages],
        check=False,
    )
    if result.returncode != 0:
        print("\npip install failed. Fix the error above and re-run.")
        sys.exit(1)


def ensure_dependencies() -> None:
    packages = read_requirements()
    missing = find_missing(packages)
    if not missing:
        return
    print("Imp needs the following Python packages:")
    for pkg in missing:
        print(f"  - {pkg}")
    try:
        answer = input("Install these? [y/n] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Cannot start without required packages. Exiting.")
        sys.exit(1)
    install(missing)


def start_server() -> None:
    # Imported lazily so the dependency check above runs first.
    import uvicorn

    print(f"\nImp listening on http://{HOST}:{PORT}")
    print("Open in your browser to talk to Foreman. Ctrl+C to stop.\n")
    uvicorn.run("server.app:app", host=HOST, port=PORT, log_level="info")


def main() -> None:
    check_python_version()
    ensure_dependencies()
    STATE_DIR.mkdir(exist_ok=True)
    start_server()


if __name__ == "__main__":
    main()
