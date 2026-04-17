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
3. Checks each template field against the board:
   - **missing** → create via `gh project field-create`
   - **matches** (same name + same type + equivalent options) → skip
   - **conflict** (same name, wrong type or different options) → behavior
     controlled by `--on-conflict`:
     - `stop` (default) — exit rc=2 with a structured JSON report of the
       conflicts so the Setup Agent can ask the admin what to do
     - `delete` — `gh project field-delete` the conflicting field, then
       create it fresh from the template
     - `skip` — log and proceed as-is (may cause runtime errors later
       when pipeline scripts try to write incompatible values)
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
 - 2: field conflicts detected in `stop` mode. Stdout is a JSON report
   the Setup Agent parses; config is NOT written, since we bail before
   anything irreversible happens.

Called by `server.setup_agent.do_create_imp_project` — the tool parses
this script's exit code to decide whether to report success, a blocker,
or surface conflict details to the admin for a delete-or-abort choice.
Keep the exit-code contract stable.
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
FIELDS_TEMPLATE = ROOT / "renderers" / "fields.json"

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


def delete_field(owner: str, field_id: str) -> None:
    """Remove a project field by its GraphQL node ID.

    Used only by the `on_conflict=delete` path — a type / option
    mismatch on a same-named field means we need to start fresh, since
    GitHub doesn't let you change a field's dataType in place.
    """
    rc, out = run_gh(
        [
            "gh",
            "project",
            "field-delete",
            "--id",
            field_id,
        ]
    )
    if rc != 0:
        raise RuntimeError(
            f"gh project field-delete failed for id={field_id!r} "
            f"(rc={rc}): {out.strip()}"
        )


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


# ---------- conflict detection ----------

ON_CONFLICT_CHOICES = ("stop", "delete", "skip")


def _existing_option_names(existing_field: dict[str, Any]) -> list[str]:
    """Extract option names from a field-list entry, handling both the
    `options: [...]` and `singleSelectOptions: [...]` shapes gh returns.

    An option entry may itself be either a string or a dict with a
    `name` key — we normalize to a plain list of strings.
    """
    raw_opts = (
        existing_field.get("options")
        or existing_field.get("singleSelectOptions")
        or []
    )
    out: list[str] = []
    for opt in raw_opts:
        if isinstance(opt, str):
            out.append(opt)
        elif isinstance(opt, dict):
            name = opt.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def detect_field_conflicts(
    existing_fields: list[dict[str, Any]],
    template: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare existing fields against the template — return conflicts.

    A conflict is a same-named field whose `dataType` doesn't match OR
    (for SINGLE_SELECT) whose options differ from the template's set.
    Fields that don't exist yet are NOT conflicts — they're just
    missing and will be created by `bootstrap_project`.

    Return shape (each entry is what the Setup Agent surfaces to the
    admin):
      {
        "name": "confidence",
        "reason": "wrong_type" | "wrong_options",
        "expected_type": "SINGLE_SELECT",
        "actual_type": "TEXT",
        "expected_options": ["high", "medium", "low"],
        "actual_options": [],
        "field_id": "PVTF_..."   (so on_conflict=delete can target it)
      }
    """
    by_name = {f.get("name"): f for f in existing_fields if isinstance(f.get("name"), str)}
    conflicts: list[dict[str, Any]] = []

    for tmpl in template:
        name = tmpl["name"]
        existing = by_name.get(name)
        if existing is None:
            continue  # missing is not a conflict

        expected_type = tmpl["type"]
        actual_type = existing.get("dataType") or existing.get("type")
        if actual_type != expected_type:
            conflicts.append(
                {
                    "name": name,
                    "reason": "wrong_type",
                    "expected_type": expected_type,
                    "actual_type": actual_type,
                    "field_id": existing.get("id"),
                }
            )
            continue

        if expected_type == "SINGLE_SELECT":
            expected_opts = sorted(tmpl.get("options") or [])
            actual_opts = sorted(_existing_option_names(existing))
            if expected_opts != actual_opts:
                conflicts.append(
                    {
                        "name": name,
                        "reason": "wrong_options",
                        "expected_type": expected_type,
                        "actual_type": actual_type,
                        "expected_options": list(tmpl.get("options") or []),
                        "actual_options": _existing_option_names(existing),
                        "field_id": existing.get("id"),
                    }
                )

    return conflicts


# ---------- orchestration ----------


def bootstrap_project(
    owner: str,
    title: str,
    on_conflict: str = "stop",
) -> dict[str, Any]:
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

    if on_conflict not in ON_CONFLICT_CHOICES:
        raise ValueError(
            f"on_conflict must be one of {ON_CONFLICT_CHOICES}, got {on_conflict!r}"
        )

    existing_fields = list_fields(owner, number)
    template = load_fields_template()

    conflicts = detect_field_conflicts(existing_fields, template)

    # `stop` (default): raise a structured conflict so the admin can
    # decide between delete-and-recreate or manual fix. Config is NOT
    # written — we bail before touching anything irreversible.
    if conflicts and on_conflict == "stop":
        raise ConflictError(
            conflicts=conflicts,
            project_number=number,
            project_owner=owner,
            project_status=project_status,
        )

    # `delete`: remove each conflicting field by ID, then fall through
    # to the normal missing-field creation path. Existing options /
    # values on items using these fields are lost — that's the choice
    # the admin made.
    deleted_fields: list[str] = []
    if conflicts and on_conflict == "delete":
        for conflict in conflicts:
            field_id = conflict.get("field_id")
            if not field_id:
                raise RuntimeError(
                    f"conflict for {conflict['name']!r} has no field_id — "
                    "can't delete it"
                )
            delete_field(owner, field_id)
            deleted_fields.append(conflict["name"])
        # After deletion, refresh the existing-fields list so the
        # create pass doesn't see the stale entries.
        existing_fields = list_fields(owner, number)

    # `skip`: conflicts are logged in the return dict but no write
    # happens; the admin accepts the runtime-risk.
    existing_names = {f.get("name") for f in existing_fields if isinstance(f.get("name"), str)}

    created_fields: list[str] = []
    skipped_fields: list[str] = []
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
        "deleted_fields": deleted_fields,
        "conflicts_ignored": conflicts if on_conflict == "skip" else [],
        "on_conflict": on_conflict,
    }


class ConflictError(Exception):
    """Raised when `bootstrap_project(on_conflict="stop")` finds field
    mismatches that need an admin decision. The CLI entry point catches
    this and exits `rc=2` with a JSON report; the Setup Agent parses
    that report and asks the admin what to do next.
    """

    def __init__(
        self,
        *,
        conflicts: list[dict[str, Any]],
        project_number: int,
        project_owner: str,
        project_status: str,
    ) -> None:
        super().__init__(
            f"{len(conflicts)} field conflict(s) detected on project "
            f"#{project_number}. Admin must choose: delete (overwrite) or "
            f"stop (fix manually)."
        )
        self.conflicts = conflicts
        self.project_number = project_number
        self.project_owner = project_owner
        self.project_status = project_status

    def report(self) -> dict[str, Any]:
        return {
            "status": "conflicts_detected",
            "project_number": self.project_number,
            "project_owner": self.project_owner,
            "project_status": self.project_status,
            "conflicts": self.conflicts,
            "next_steps": (
                "Re-run project_bootstrap.py with --on-conflict delete to "
                "overwrite the conflicting fields (destructive — values on "
                "existing items in those fields will be lost), or fix the "
                "fields manually in the GitHub UI and re-run with the "
                "default --on-conflict stop."
            ),
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
    parser.add_argument(
        "--on-conflict",
        choices=ON_CONFLICT_CHOICES,
        default="stop",
        help=(
            "What to do if a same-named field has the wrong type or different "
            "single-select options: 'stop' (default, exit rc=2 with a report), "
            "'delete' (remove + recreate — destructive), or 'skip' (accept "
            "the existing field as-is, risks runtime errors later)."
        ),
    )
    args = parser.parse_args()

    try:
        result = bootstrap_project(
            owner=args.owner,
            title=args.title,
            on_conflict=args.on_conflict,
        )
    except ConflictError as exc:
        # rc=2: conflicts detected in stop mode — stdout is JSON the
        # Setup Agent parses to surface the mismatch to the admin.
        print(json.dumps(exc.report(), indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 — propagate message to Setup Agent
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
