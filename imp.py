#!/usr/bin/env python3
"""Imp — entry point.

Starts the Imp web service.

  1. Bootstrap a project-local virtual environment at .venv/ on first run,
     then re-exec inside it so the rest of the script runs against private
     dependencies.
  2. Install any missing required packages into the private venv. No prompt:
     the venv is private to this project so there's nothing to ask permission
     about.
  3. Start uvicorn against `server.app:app`.
  4. Print the URL.

After this, the terminal only shows logs. All further configuration happens
in the browser via the Setup Agent.

Set ``IMP_USE_SYSTEM_PYTHON=1`` to skip the venv bootstrap and use the active
interpreter — for Docker images and similar environments where Python is
already managed externally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED_PYTHON = (3, 11)
ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS_FILE = ROOT / "requirements.txt"
STATE_DIR = ROOT / ".imp"
HOST = "127.0.0.1"
PORT = 8421

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


# ---------- venv bootstrap ----------

def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def in_our_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR


def venv_is_healthy() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    result = subprocess.run(
        [str(py), "-c", "import sys"],
        capture_output=True,
    )
    return result.returncode == 0


def bootstrap_venv() -> None:
    """Create .venv/ if missing and re-exec inside it.

    No-op if IMP_USE_SYSTEM_PYTHON=1 or if we are already running inside
    our own venv. After re-exec, the new process re-enters this function,
    sees ``in_our_venv()`` is True, and returns immediately.
    """
    if os.environ.get("IMP_USE_SYSTEM_PYTHON") == "1":
        if Path(sys.prefix) == Path(sys.base_prefix):
            print(
                "Warning: IMP_USE_SYSTEM_PYTHON=1 set but no venv active. "
                "pip will install into the system Python.",
                flush=True,
            )
        return

    if in_our_venv():
        return

    if VENV_DIR.exists() and not venv_is_healthy():
        print("Existing .venv/ looks broken; recreating.", flush=True)
        shutil.rmtree(VENV_DIR)

    if not VENV_DIR.exists():
        print("Creating .venv/ (one-time setup)...", flush=True)
        import venv

        venv.create(VENV_DIR, with_pip=True)

    # Force unbuffered stdout in the re-exec'd process so its log lines stay
    # interleaved correctly with subprocess output (pip, uvicorn).
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Re-exec inside our venv. os.execv replaces the current process, so the
    # user sees one continuous run with no second prompt.
    py = venv_python()
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


# ---------- dependency management ----------

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
    import importlib.util

    missing: list[str] = []
    for pkg in packages:
        import_name = IMPORT_NAME_OVERRIDES.get(pkg, pkg.replace("-", "_"))
        if importlib.util.find_spec(import_name) is None:
            missing.append(pkg)
    return missing


def install(packages: list[str]) -> None:
    print(f"Installing: {', '.join(packages)}", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--compile", *packages],
        check=False,
    )
    if result.returncode != 0:
        print("\npip install failed. Fix the error above and re-run.", flush=True)
        sys.exit(1)
    # Pre-compile bytecode for the freshly-installed packages so the very
    # first chainlit run isn't a 30-60s "looks hung" wait while macOS
    # Python compiles aiohttp / chainlit / literalai / traceloop on demand.
    print("Compiling bytecode (one-time)...", flush=True)
    site_packages = (
        Path(sys.executable).parent.parent
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if site_packages.exists():
        subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "-j", "0", str(site_packages)],
            check=False,
        )


def ensure_dependencies() -> None:
    packages = read_requirements()
    missing = find_missing(packages)
    if missing:
        install(missing)


# ---------- server launch ----------

def start_server() -> None:
    print(f"\nStarting Imp at http://{HOST}:{PORT}", flush=True)
    print("Ctrl+C to stop.\n", flush=True)

    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m", "uvicorn",
            "server.render_route:app",
            "--host", HOST,
            "--port", str(PORT),
            "--log-level", "warning",
        ],
    )


def main() -> None:
    check_python_version()
    bootstrap_venv()
    ensure_dependencies()
    STATE_DIR.mkdir(exist_ok=True)
    start_server()


if __name__ == "__main__":
    main()
