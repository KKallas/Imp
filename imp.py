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

def ensure_chainlit_secret() -> str:
    """Generate and persist a CHAINLIT_AUTH_SECRET on first run."""
    secret_file = STATE_DIR / "chainlit_secret"
    if secret_file.exists():
        return secret_file.read_text().strip()
    import secrets
    secret = secrets.token_urlsafe(32)
    STATE_DIR.mkdir(exist_ok=True)
    secret_file.write_text(secret)
    secret_file.chmod(0o600)
    return secret


def ensure_admin_password() -> None:
    """Prompt the admin to set a password on very first run.

    Runs only if .imp/config.json does not already contain
    `admin_password_hash`. Uses `getpass` so the password is never echoed
    to the terminal, and requires double-entry confirmation. The plaintext
    never leaves this function — we hash it with argon2id and persist only
    the hash.

    By the time this returns, Chainlit's auth callback is guaranteed to
    have a hash to verify against, so there is no "bootstrap mode" and no
    possibility of the password being entered in the browser chat (where
    it would otherwise appear in the chat log).
    """
    import getpass
    import json

    cfg_file = STATE_DIR / "config.json"
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text())
        except json.JSONDecodeError:
            cfg = {}

    if cfg.get("admin_password_hash"):
        return  # Already set.

    from argon2 import PasswordHasher

    print("\nFirst-run admin password setup", flush=True)
    print(
        "This runs once. Imp will hash the password with argon2id and "
        "store only the hash in .imp/config.json. The plaintext is never "
        "written to disk or sent to the browser.",
        flush=True,
    )
    while True:
        try:
            pw1 = getpass.getpass("Choose an admin password: ")
        except EOFError:
            print(
                "\nNo TTY for password input. Set IMP_ADMIN_PASSWORD in the "
                "environment once to seed an initial password non-interactively, "
                "then unset it. Aborting.",
                flush=True,
            )
            sys.exit(1)
        if len(pw1) < 4:
            print("Too short — minimum 4 characters.", flush=True)
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords don't match. Try again.", flush=True)
            continue
        break

    hashed = PasswordHasher().hash(pw1)
    cfg["admin_password_hash"] = hashed
    STATE_DIR.mkdir(exist_ok=True)
    cfg_file.write_text(json.dumps(cfg, indent=2))
    cfg_file.chmod(0o600)
    print("Admin password set.\n", flush=True)


def ensure_admin_password_from_env() -> None:
    """Alternate path for non-interactive deployments.

    If IMP_ADMIN_PASSWORD is set in the env and no hash exists yet, seed
    the hash from that value and exit. Intended for one-time seeding from
    an infrastructure-provisioning script on a headless host, NOT as a
    runtime password source.
    """
    import json

    env_pw = os.environ.get("IMP_ADMIN_PASSWORD")
    if not env_pw:
        return

    cfg_file = STATE_DIR / "config.json"
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text())
        except json.JSONDecodeError:
            cfg = {}
    if cfg.get("admin_password_hash"):
        return

    if len(env_pw) < 4:
        print("IMP_ADMIN_PASSWORD is set but too short (min 4). Ignoring.", flush=True)
        return

    from argon2 import PasswordHasher

    cfg["admin_password_hash"] = PasswordHasher().hash(env_pw)
    STATE_DIR.mkdir(exist_ok=True)
    cfg_file.write_text(json.dumps(cfg, indent=2))
    cfg_file.chmod(0o600)
    print("Admin password seeded from IMP_ADMIN_PASSWORD env var.", flush=True)
    print("You should unset IMP_ADMIN_PASSWORD now — it is only needed for first-run seeding.", flush=True)


def start_server() -> None:
    secret = ensure_chainlit_secret()
    os.environ["CHAINLIT_AUTH_SECRET"] = secret

    # Seed-from-env path first (headless deploys), then interactive path
    # (human running it from a terminal).
    ensure_admin_password_from_env()
    ensure_admin_password()

    print("\nLoading chainlit...", flush=True)
    print(
        "(First run can take 30-60s on macOS while Python compiles bytecode "
        "for the chainlit dep tree. Subsequent runs are fast. Don't Ctrl+C "
        "unless it's been over a minute with no output.)",
        flush=True,
    )
    print(f"\nWill be available at http://{HOST}:{PORT}", flush=True)
    print("Ctrl+C to stop.\n", flush=True)

    main_py = ROOT / "main.py"
    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "chainlit",
            "run",
            str(main_py),
            "--host", HOST,
            "--port", str(PORT),
            "--headless",
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
