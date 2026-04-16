#!/usr/bin/env python3
"""pipeline/estimate_dates.py — Layer 1 of the gantt flow.

Most real repos don't have an attached GH Project, which means
`sync_issues.py` can't populate `start_date` / `end_date` project
fields — and `render_chart.py --template gantt` then shows 0 bars and
a 35-issue "missing dates" list. This script fixes that by running
`pipeline.scenarios.synthesize_dates` against the enriched payload
and (optionally) persisting the estimates back to each GH issue body
as a clearly-marked machine-managed block, so they survive the next
sync and anyone looking at the issue on github.com sees the same
estimate the chart draws from.

## Two modes

  - **Default** (no flag): synthesize dates, update `.imp/enriched.json`
    in place, exit. Pure local — safe for CI, dry-runs, and testing.

  - **`--push`**: also edit every issue whose dates we synthesized,
    updating its body to include an `<!-- imp:dates:begin -->` /
    `<!-- imp:dates:end -->` block with the new values. Idempotent —
    re-running replaces the block rather than appending.

## Body-block format

    <!-- imp:dates:begin -->
    <!-- Managed by pipeline/estimate_dates.py — do not edit this block. -->
    start_date: 2026-04-11
    end_date: 2026-04-14
    duration_days: 3
    <!-- imp:dates:end -->

`sync_issues.py` parses this block back into the issue's `fields` dict
on the next sync, so the round-trip is seamless. `heuristics.py` then
re-wraps the values with `source: "synthesized"` provenance so
downstream renderers can still distinguish estimated from real
project-board data.

## Read-only without --push

No GitHub side effects in the default mode. Classified as a read by
`server/intercept.py` (see `PIPELINE_READ_SCRIPTS`). The `--push`
path goes through `gh issue edit --body` per-issue, which hits the
guard + edits budget like any other Foreman write.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENRICHED_FILE = ROOT / ".imp" / "enriched.json"


# ---------- body-block format ----------

BLOCK_BEGIN = "<!-- imp:dates:begin -->"
BLOCK_END = "<!-- imp:dates:end -->"
BLOCK_HEADER_COMMENT = (
    "<!-- Managed by pipeline/estimate_dates.py — do not edit this block. -->"
)

# Fields we round-trip through the body block. Keep this tight: we
# don't want to accidentally start persisting heuristic guesses for
# fields the user might manage by hand.
_PERSISTED_FIELDS: tuple[str, ...] = ("start_date", "end_date", "duration_days")

# Regex that matches the whole block including markers. DOTALL so `.`
# eats newlines; non-greedy so two blocks (shouldn't happen but)
# don't merge.
_BLOCK_RE = re.compile(
    re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END),
    re.DOTALL,
)


def render_body_block(fields: dict[str, Any]) -> str:
    """Render a body block for the subset of `fields` we persist.

    `fields` is the flat value-per-key form (already unwrapped from
    the provenance envelope). Missing keys are skipped — we don't
    emit empty rows.
    """
    lines: list[str] = [BLOCK_BEGIN, BLOCK_HEADER_COMMENT]
    for key in _PERSISTED_FIELDS:
        value = fields.get(key)
        if value in (None, ""):
            continue
        lines.append(f"{key}: {value}")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def upsert_body_block(body: str, new_block: str) -> str:
    """Return `body` with the dates block replaced (or appended).

    Preserves everything outside the block verbatim, including
    leading/trailing whitespace patterns the user has chosen."""
    if _BLOCK_RE.search(body or ""):
        return _BLOCK_RE.sub(new_block, body or "")
    # Append with a one-line gap so it sits cleanly at the bottom.
    existing = (body or "").rstrip()
    if existing:
        return f"{existing}\n\n{new_block}\n"
    return f"{new_block}\n"


# ---------- field helpers ----------


def _unwrap(envelope: Any) -> Any:
    """Pull the `value` out of a provenance envelope, or pass through."""
    if isinstance(envelope, dict) and "value" in envelope:
        return envelope["value"]
    return envelope


def _field_was_synthesized(issue: dict[str, Any], key: str) -> bool:
    """Did this run (or a prior one) flag this field as synthesized?"""
    envelope = (issue.get("fields") or {}).get(key)
    if isinstance(envelope, dict):
        return envelope.get("source") == "synthesized"
    return False


def _flat_persisted_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Extract the {start_date, end_date, duration_days} trio as flat
    values for the body block. Returns only keys that have values."""
    out: dict[str, Any] = {}
    for key in _PERSISTED_FIELDS:
        value = _unwrap((issue.get("fields") or {}).get(key))
        if value not in (None, ""):
            out[key] = value
    return out


# ---------- gh push ----------


def run_gh(argv: list[str]) -> tuple[int, str, str]:
    """Invoke `gh` — seam for tests to mock with a fake gh."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def push_issue_body(
    repo: str, number: int, new_body: str
) -> tuple[bool, str]:
    """Write `new_body` to issue `#number`. Returns (ok, message)."""
    rc, stdout, stderr = run_gh(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            repo,
            "--body",
            new_body,
        ]
    )
    if rc != 0:
        detail = (stderr.strip() or stdout.strip() or "")[:200]
        return False, f"gh issue edit #{number} failed (rc={rc}): {detail}"
    return True, f"updated #{number}"


# ---------- main flow ----------


def estimate_in_place(
    enriched: dict[str, Any], *, today: date | None = None
) -> tuple[dict[str, Any], list[int]]:
    """Run `synthesize_dates` over `enriched` and return (updated,
    issue_numbers_with_new_synthesis).

    The second tuple member lists issues where at least one of the
    persisted fields is newly marked as synthesized — i.e. the set of
    issues we'd push to GH if `--push` is on.
    """
    # Local import — heuristics/scenarios pull the full pipeline tree
    # and we don't want to inflate import time when tests only need
    # the body-block helpers. Try the package path (tests importing
    # via `pipeline.scenarios`) and fall back to the sibling-module
    # path (CLI invocation with pipeline/ on sys.path).
    try:
        from pipeline import scenarios
    except ImportError:
        import scenarios  # type: ignore[no-redef]

    before_status: dict[int, dict[str, bool]] = {}
    for issue in enriched.get("issues") or []:
        n = issue.get("number")
        if isinstance(n, int):
            before_status[n] = {
                "start_date": _field_was_synthesized(issue, "start_date"),
                "end_date": _field_was_synthesized(issue, "end_date"),
            }

    updated = scenarios.synthesize_dates(enriched, today=today)

    touched: list[int] = []
    for issue in updated.get("issues") or []:
        n = issue.get("number")
        if not isinstance(n, int):
            continue
        before = before_status.get(n, {"start_date": False, "end_date": False})
        now_synth = {
            "start_date": _field_was_synthesized(issue, "start_date"),
            "end_date": _field_was_synthesized(issue, "end_date"),
        }
        # Newly-synthesized = wasn't marked as synthesized before this
        # pass but is now. Issues with real project-board dates never
        # appear here because synthesize_dates leaves them alone.
        if any(now_synth[k] and not before[k] for k in now_synth):
            touched.append(n)

    updated["estimated_at"] = datetime.now(timezone.utc).isoformat()
    return updated, touched


def load_enriched(path: Path = ENRICHED_FILE) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `pipeline/sync_issues.py` + "
            f"`pipeline/heuristics.py` first"
        )
    return json.loads(path.read_text())


def write_enriched(payload: dict[str, Any], path: Path = ENRICHED_FILE) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def push_to_github(
    enriched: dict[str, Any], touched_numbers: list[int]
) -> dict[str, Any]:
    """Write synthesised dates back to GH issue bodies.

    Returns a report dict with per-issue outcomes for the caller to
    surface to the Foreman agent / admin.
    """
    repo = enriched.get("repo")
    if not repo:
        return {"error": "enriched.json has no `repo` — cannot push"}

    by_num = {
        i.get("number"): i
        for i in enriched.get("issues") or []
        if isinstance(i.get("number"), int)
    }

    successes: list[str] = []
    failures: list[str] = []
    for number in touched_numbers:
        issue = by_num.get(number)
        if issue is None:
            failures.append(f"#{number}: not found in enriched payload")
            continue
        fields = _flat_persisted_fields(issue)
        if not fields:
            failures.append(f"#{number}: no persisted fields to write")
            continue
        body = issue.get("body") or ""
        new_body = upsert_body_block(body, render_body_block(fields))
        ok, message = push_issue_body(repo, number, new_body)
        (successes if ok else failures).append(message)

    return {
        "pushed": len(successes),
        "failed": len(failures),
        "successes": successes,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=ENRICHED_FILE,
        help=f"Path to enriched.json (default {ENRICHED_FILE})",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help=(
            "After estimating, write each synthesised issue's new dates "
            "back to GH via `gh issue edit --body` (upserts an "
            "<!-- imp:dates --> block). Off by default — no GH side "
            "effects unless this flag is set."
        ),
    )
    args = parser.parse_args()

    try:
        enriched = load_enriched(args.input)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1

    updated, touched = estimate_in_place(enriched)
    write_enriched(updated, args.input)

    print(
        f"Estimated dates for {len(touched)} issue(s) → {args.input}",
        file=sys.stderr,
    )

    if args.push and touched:
        report = push_to_github(updated, touched)
        if "error" in report:
            print(report["error"], file=sys.stderr)
            return 1
        for line in report["successes"]:
            print(f"  + {line}", file=sys.stderr)
        for line in report["failures"]:
            print(f"  ! {line}", file=sys.stderr)
        print(
            f"Pushed {report['pushed']}, {report['failed']} failed.",
            file=sys.stderr,
        )
        if report["failed"] and not report["pushed"]:
            return 1
    elif args.push:
        print("No issues needed updating — nothing to push.", file=sys.stderr)

    # Final machine-readable line on stdout so Foreman's path parser
    # (in server/foreman_agent.py) can pick up the count.
    print(json.dumps({"estimated": len(touched), "pushed": args.push}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
