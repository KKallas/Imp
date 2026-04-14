#!/usr/bin/env python3
"""pipeline/project_bootstrap.py — provision the Imp Projects-v2 board.

The script the Setup Agent calls (via `server.setup_agent.do_create_imp_project`)
to stand up the admin's Projects-v2 board on first run and verify it on
subsequent runs. Idempotent: safe to re-run; it'll find the existing
board, skip fields that already exist, and only create the gaps.

## What it does

1. Finds or creates a Projects-v2 board titled `<--title>` (default `Imp`)
   under `<--owner>` (user or org login).
2. Reads the canonical field definitions from `templates/fields.json`.
3. For each field that doesn't already exist on the board, creates it via
   `gh project field-create` — preserving `--single-select-options` where
   applicable.
4. Persists `project_number` and `project_owner` to `.imp/config.json`
   so the worker and pipeline scripts know which board to talk to.

## Prerequisites

`gh auth status` must be green AND the token must have the `project`
scope (the default scope on `gh auth login --web` includes it on recent
versions). If the scope is missing, `gh project` calls fail with a
scope error — re-run `gh auth refresh -s project` and try again.

## Exit codes

 - 0: everything provisioned (or already present) and config written.
 - 1: gh CLI error or JSON parse failure (stderr has the specific gh
   output so the Setup Agent can surface it to the admin).

Called by `server.setup_agent.do_create_imp_project` — the tool parses
this script's exit code to decide whether to report success or a
blocker to the admin. Keep the exit-code contract stable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".imp" / "config.json"
FIELDS_TEMPLATE = ROOT / "templates" / "fields.json"

GH_PROJECT_LIST_LIMIT = 100
GH_FIELD_LIST_LIMIT = 100


# ---------- gh runner (seam for tests) ----------


def run_gh(argv: list[str]) -> tuple[int, str]:
    """Run a gh command, return (returncode, combined stdout+stderr).

    Tests monkey-patch this module-level name so they can script
    responses without a real gh binary.
    """
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------- config I/O ----------


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def load_fields_template() -> list[dict[str, Any]]:
    data = json.loads(FIELDS_TEMPLATE.read_text())
    fields = data.get("fields")
    if not isinstance(fields, list):
        raise ValueError(
            f"templates/fields.json: missing or malformed 'fields' list: {data!r}"
        )
    return fields


# ---------- gh project operations ----------


def find_project(owner: str, title: str) -> dict[str, Any] | None:
    """Return the project dict with `title == <title>` under `owner`, or None."""
    rc, out = run_gh(
        [
            "gh",
            "project",
            "list",
            "--owner",
            owner,
            "--format",
            "json",
            "--limit",
            str(GH_PROJECT_LIST_LIMIT),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"gh project list failed (rc={rc}): {out.strip()}")

    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh project list: unparseable JSON: {exc}; raw: {out[:300]!r}"
        ) from exc

    projects = data.get("projects", []) if isinstance(data, dict) else data
    for p in projects:
        if isinstance(p, dict) and p.get("title") == title:
            return p
    return None


def create_project(owner: str, title: str) -> dict[str, Any]:
    """Create a new Projects-v2 board and return its JSON dict."""
    rc, out = run_gh(
        [
            "gh",
            "project",
            "create",
            "--owner",
            owner,
            "--title",
            title,
            "--format",
            "json",
        ]
    )
    if rc != 0:
        raise RuntimeError(f"gh project create failed (rc={rc}): {out.strip()}")
    try:
        return json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh project create: unparseable JSON: {exc}; raw: {out[:300]!r}"
        ) from exc


def list_fields(owner: str, number: int) -> list[dict[str, Any]]:
    rc, out = run_gh(
        [
            "gh",
            "project",
            "field-list",
            str(number),
            "--owner",
            owner,
            "--format",
            "json",
            "--limit",
            str(GH_FIELD_LIST_LIMIT),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"gh project field-list failed (rc={rc}): {out.strip()}")

    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh project field-list: unparseable JSON: {exc}; raw: {out[:300]!r}"
        ) from exc

    fields = data.get("fields", []) if isinstance(data, dict) else data
    return [f for f in fields if isinstance(f, dict)]


def create_field(owner: str, number: int, field_def: dict[str, Any]) -> None:
    """Create a single custom field on the board.

    Single-select fields also get their option list via
    `--single-select-options one,two,three`.
    """
    argv = [
        "gh",
        "project",
        "field-create",
        str(number),
        "--owner",
        owner,
        "--name",
        field_def["name"],
        "--data-type",
        field_def["type"],
    ]
    if field_def["type"] == "SINGLE_SELECT":
        options = field_def.get("options") or []
        if not options:
            raise ValueError(
                f"field {field_def['name']!r} is SINGLE_SELECT but has no 'options'"
            )
        argv.extend(["--single-select-options", ",".join(options)])

    rc, out = run_gh(argv)
    if rc != 0:
        raise RuntimeError(
            f"gh project field-create failed for {field_def['name']!r} "
            f"(rc={rc}): {out.strip()}"
        )


# ---------- orchestration ----------


def bootstrap_project(owner: str, title: str) -> dict[str, Any]:
    """Idempotently provision the board + fields for `owner`.

    Returns a summary dict the CLI entry point prints to stdout and the
    Setup Agent tool body surfaces in its `output` field.
    """
    existing = find_project(owner, title)
    if existing:
        number = existing.get("number")
        project_status = "existing"
    else:
        created = create_project(owner, title)
        number = created.get("number")
        project_status = "created"

    if not isinstance(number, int):
        raise RuntimeError(
            f"gh didn't return an integer project number (got {number!r}); "
            f"aborting before writing config"
        )

    existing_fields = list_fields(owner, number)
    existing_names = {f.get("name") for f in existing_fields}

    created_fields: list[str] = []
    skipped_fields: list[str] = []
    template = load_fields_template()
    for field_def in template:
        if field_def["name"] in existing_names:
            skipped_fields.append(field_def["name"])
            continue
        create_field(owner, number, field_def)
        created_fields.append(field_def["name"])

    cfg = load_config()
    cfg["project_number"] = number
    cfg["project_owner"] = owner
    save_config(cfg)

    return {
        "project_number": number,
        "project_owner": owner,
        "project_status": project_status,
        "created_fields": created_fields,
        "skipped_fields": skipped_fields,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub owner (user or org login) that will own the project",
    )
    parser.add_argument(
        "--title",
        default="Imp",
        help="Project title (default: Imp)",
    )
    args = parser.parse_args()

    try:
        result = bootstrap_project(owner=args.owner, title=args.title)
    except Exception as exc:  # noqa: BLE001 — propagate message to Setup Agent
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
