#!/usr/bin/env python3
"""pipeline/sync_issues.py — pull issues + project field values from GitHub.

The visibility-pipeline entry point. Fetches every issue from the
configured repo, fetches every item from the configured Projects-v2
board (if there is one), merges them on issue number, and writes the
result to `.imp/issues.json` for downstream pipeline scripts
(heuristics.py, render_chart.py, scenario.py).

## Inputs

  - `.imp/config.json` for `repo`, `project_number`, `project_owner`.
    Set by the Setup Agent (P3.9) and project_bootstrap (P3.10).

## Output shape (.imp/issues.json)

  {
    "synced_at": "<ISO 8601 UTC timestamp>",
    "repo": "<owner/name>",
    "project_number": <int|null>,
    "project_owner": "<owner|null>",
    "issue_count": <int>,
    "issues": [
      {
        "number": 42,
        "title": "...",
        "body": "...",
        "labels": [{"name": "area:server", ...}],
        "milestone": {"title": "Phase 4 ..."},
        "assignees": [{"login": "..."}],
        "state": "OPEN" | "CLOSED",
        "url": "...",
        "createdAt": "...",
        "updatedAt": "...",
        "fields": {              # custom Projects v2 field values, or {}
          "duration_days": 5,    # NUMBER → number
          "start_date": "...",   # DATE   → ISO YYYY-MM-DD
          "confidence": "high",  # SINGLE_SELECT → option name
          "depends_on": "..."    # TEXT
        }
      },
      ...
    ]
  }

## Pagination

  `gh issue list --limit N` and `gh project item-list --limit N` ask
  the gh CLI to paginate up to N results. We default `--limit 1000`,
  which covers most repos. Set `--limit` higher if you have more.

## Read-only

  No `gh` writes are issued. The script can run on a freshly-set-up
  repo without any guard / budget concern. It IS classified as a read
  by `server/intercept.py` (PIPELINE_READ_SCRIPTS) — the Foreman
  agent's `run_sync_issues` tool calls it without burning the edits
  or tasks budget.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / ".imp" / "config.json"
OUTPUT_FILE = ROOT / ".imp" / "issues.json"

DEFAULT_LIMIT = 1000

# Fields we ask `gh issue list` to populate. Picked to match the AC
# (number, title, body, labels, milestone, assignees, state) plus a
# few extras (url + createdAt + updatedAt) that downstream scripts
# need for chart rendering and stale-data detection.
ISSUE_JSON_FIELDS = (
    "number,title,body,labels,milestone,assignees,state,url,createdAt,updatedAt"
)


# ---------- gh runner (seam for tests) ----------


def run_gh(argv: list[str]) -> tuple[int, str, str]:
    """Run a gh command, return (returncode, stdout, stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ---------- config I/O ----------


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


# ---------- gh fetchers ----------


def fetch_issues(repo: str, *, limit: int = DEFAULT_LIMIT, state: str = "all") -> list[dict[str, Any]]:
    rc, stdout, stderr = run_gh(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            ISSUE_JSON_FIELDS,
        ]
    )
    if rc != 0:
        raise RuntimeError(
            f"gh issue list failed (rc={rc}): {stderr.strip() or stdout.strip()}"
        )
    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh issue list: unparseable JSON: {exc}; raw: {stdout[:300]!r}"
        ) from exc
    if not isinstance(data, list):
        raise RuntimeError(
            f"gh issue list returned non-list payload: {type(data).__name__}"
        )
    return data


def fetch_project_items(
    project_number: int, owner: str, *, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    rc, stdout, stderr = run_gh(
        [
            "gh",
            "project",
            "item-list",
            str(project_number),
            "--owner",
            owner,
            "--limit",
            str(limit),
            "--format",
            "json",
        ]
    )
    if rc != 0:
        raise RuntimeError(
            f"gh project item-list failed (rc={rc}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh project item-list: unparseable JSON: {exc}; "
            f"raw: {stdout[:300]!r}"
        ) from exc
    items = data.get("items", []) if isinstance(data, dict) else data
    return [it for it in items if isinstance(it, dict)]


# ---------- merge ----------

# Keys that gh project item-list emits at the item level that ARE NOT
# custom fields — they're metadata about the item itself. Anything else
# at the top level is treated as a custom field value.
_ITEM_RESERVED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "type",
        "content",
        "status",  # Status is a default Projects v2 field, kept under custom fields
    }
)


def _normalize_field_value(raw: Any) -> Any:
    """Reduce a `gh project item-list` field cell to a scalar.

    For most field types gh inlines a primitive (number, string, ISO
    date) directly, but defensively we also handle dict-shaped cells
    in case a future gh version changes the format.
    """
    if isinstance(raw, dict):
        for key in ("number", "date", "name", "text", "value"):
            if key in raw:
                return raw[key]
        return raw  # unknown shape — pass through verbatim
    return raw


def merge_issues_with_fields(
    issues: list[dict[str, Any]],
    project_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach project field values to each issue by issue number.

    Mutates `issues` in place — adds a `"fields": {...}` key to every
    issue dict. Issues not on the project board get `"fields": {}`.
    PR items on the board are ignored.
    """
    fields_by_number: dict[int, dict[str, Any]] = {}
    for item in project_items:
        content = item.get("content") or {}
        if content.get("type") != "Issue":
            continue
        num = content.get("number")
        if not isinstance(num, int):
            continue
        custom: dict[str, Any] = {}
        for key, value in item.items():
            if key in _ITEM_RESERVED_KEYS:
                continue
            custom[key] = _normalize_field_value(value)
        fields_by_number[num] = custom

    for issue in issues:
        num = issue.get("number")
        if isinstance(num, int):
            issue["fields"] = fields_by_number.get(num, {})
        else:
            issue["fields"] = {}

    return issues


# ---------- orchestration ----------


def sync(
    *,
    limit: int = DEFAULT_LIMIT,
    state: str = "all",
) -> dict[str, Any]:
    """Run the full sync. Returns the JSON dict that's also written to
    `.imp/issues.json`."""
    cfg = load_config()
    repo = cfg.get("repo")
    if not repo:
        raise RuntimeError(
            "no `repo` in .imp/config.json — run the Setup Agent first"
        )

    issues = fetch_issues(repo, limit=limit, state=state)

    project_number = cfg.get("project_number")
    project_owner = cfg.get("project_owner") or _owner_from_repo(repo)

    if isinstance(project_number, int) and project_owner:
        items = fetch_project_items(project_number, project_owner, limit=limit)
        merge_issues_with_fields(issues, items)
    else:
        # No project board configured — every issue gets an empty
        # fields dict so downstream scripts can rely on the key existing.
        for issue in issues:
            issue["fields"] = {}

    return {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "project_number": project_number,
        "project_owner": project_owner,
        "issue_count": len(issues),
        "issues": issues,
    }


def _owner_from_repo(repo: str) -> str | None:
    """Extract `owner` from `owner/name` — fallback when project_owner
    isn't set explicitly in config."""
    if "/" in repo:
        return repo.split("/", 1)[0]
    return None


def write_output(payload: dict[str, Any], path: Path = OUTPUT_FILE) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max issues / project items to fetch (default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--state",
        default="all",
        choices=["open", "closed", "all"],
        help="Issue state filter (default: all)",
    )
    args = parser.parse_args()

    try:
        payload = sync(limit=args.limit, state=args.state)
    except Exception as exc:  # noqa: BLE001 — surface to caller (Foreman)
        print(str(exc), file=sys.stderr)
        return 1

    write_output(payload)
    print(
        f"Synced {payload['issue_count']} issues from {payload['repo']} "
        f"→ {OUTPUT_FILE}",
        file=sys.stderr,
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
