"""Tests for pipeline/sync_issues.py.

Run directly: `.venv/bin/python tests/test_sync_issues.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Mocks `sync_issues.run_gh` with a scripted FakeGh — no real `gh` binary,
no GitHub API calls.

`CONFIG_FILE` and `OUTPUT_FILE` are redirected to a tempdir so the
shared `.imp/` is never touched.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import sync_issues as si  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-sync-test-"))
si.CONFIG_FILE = _TMP_DIR / "config.json"
si.OUTPUT_FILE = _TMP_DIR / "issues.json"


# ---------- fake gh ----------


class FakeGh:
    """Argv-pattern matched scripted responses (rc, stdout, stderr).

    Same shape as project_bootstrap's FakeGh — patterns are ordered
    subsequences in argv, None matches any token.
    """

    def __init__(
        self, responses: list[tuple[list[str], int, str, str]]
    ) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def _matches(self, argv: list[str], pattern: list[str]) -> bool:
        i = 0
        for tok in argv:
            if i >= len(pattern):
                return True
            if pattern[i] is None or pattern[i] == tok:
                i += 1
        return i >= len(pattern)

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for idx, (pattern, rc, stdout, stderr) in enumerate(self.responses):
            if self._matches(argv, pattern):
                self.responses.pop(idx)
                return (rc, stdout, stderr)
        raise AssertionError(
            f"FakeGh had no scripted response for argv={argv!r}; "
            f"remaining patterns={[p for p, _, _, _ in self.responses]!r}"
        )


def _reset() -> None:
    for path in (si.CONFIG_FILE, si.OUTPUT_FILE):
        if path.exists():
            path.unlink()


def _write_config(cfg: dict) -> None:
    si.CONFIG_FILE.parent.mkdir(exist_ok=True)
    si.CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------- helpers / utilities ----------


def test_normalize_field_value_passes_scalars() -> None:
    _reset()
    assert si._normalize_field_value(5) == 5
    assert si._normalize_field_value("high") == "high"
    assert si._normalize_field_value("2026-04-01") == "2026-04-01"
    assert si._normalize_field_value(None) is None
    print("test_normalize_field_value_passes_scalars: OK")


def test_normalize_field_value_unwraps_dict_shapes() -> None:
    """Defensive — handle dict cells in case gh changes the format."""
    _reset()
    assert si._normalize_field_value({"number": 5}) == 5
    assert si._normalize_field_value({"date": "2026-04-01"}) == "2026-04-01"
    assert si._normalize_field_value({"name": "high"}) == "high"
    assert si._normalize_field_value({"text": "hi"}) == "hi"
    # Unknown shape passes through unchanged
    assert si._normalize_field_value({"weird": 1}) == {"weird": 1}
    print("test_normalize_field_value_unwraps_dict_shapes: OK")


def test_owner_from_repo() -> None:
    _reset()
    assert si._owner_from_repo("KKallas/Imp") == "KKallas"
    assert si._owner_from_repo("nostrasht") is None
    print("test_owner_from_repo: OK")


# ---------- fetch ----------


def test_fetch_issues_builds_argv_and_parses_json() -> None:
    _reset()
    payload = json.dumps(
        [
            {
                "number": 42,
                "title": "P4.11",
                "body": "...",
                "labels": [{"name": "area:server"}],
                "milestone": {"title": "Phase 4"},
                "assignees": [],
                "state": "OPEN",
                "url": "...",
                "createdAt": "2026-04-11T12:30:18Z",
                "updatedAt": "2026-04-15T06:00:00Z",
            }
        ]
    )
    fake = FakeGh([(["gh", "issue", "list"], 0, payload, "")])
    si.run_gh = fake

    issues = si.fetch_issues("KKallas/Imp", limit=500, state="open")
    assert len(issues) == 1
    assert issues[0]["number"] == 42

    call = fake.calls[0]
    assert "--repo" in call and "KKallas/Imp" in call
    assert "--state" in call and "open" in call
    assert "--limit" in call and "500" in call
    assert "--json" in call
    print("test_fetch_issues_builds_argv_and_parses_json: OK")


def test_fetch_issues_surfaces_gh_error() -> None:
    _reset()
    fake = FakeGh(
        [(["gh", "issue", "list"], 1, "", "HTTP 401: Bad credentials")]
    )
    si.run_gh = fake
    try:
        si.fetch_issues("KKallas/Imp")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "401" in str(exc)
    print("test_fetch_issues_surfaces_gh_error: OK")


def test_fetch_project_items_parses_items_list() -> None:
    _reset()
    payload = json.dumps(
        {
            "items": [
                {
                    "id": "PVTI_1",
                    "title": "...",
                    "type": "Issue",
                    "content": {"type": "Issue", "number": 42, "title": "..."},
                    "duration_days": 5,
                    "confidence": "high",
                }
            ]
        }
    )
    fake = FakeGh([(["gh", "project", "item-list"], 0, payload, "")])
    si.run_gh = fake
    items = si.fetch_project_items(7, "KKallas", limit=200)
    assert len(items) == 1
    assert items[0]["duration_days"] == 5
    call = fake.calls[0]
    assert "7" in call and "KKallas" in call
    assert "--limit" in call and "200" in call
    print("test_fetch_project_items_parses_items_list: OK")


# ---------- merge ----------


def test_merge_attaches_fields_by_issue_number() -> None:
    _reset()
    issues = [
        {"number": 42, "title": "A"},
        {"number": 43, "title": "B"},
    ]
    items = [
        {
            "id": "PVTI_1",
            "title": "A",
            "type": "Issue",
            "content": {"type": "Issue", "number": 42},
            "duration_days": 5,
            "confidence": "high",
        }
    ]
    out = si.merge_issues_with_fields(issues, items)
    by_num = {i["number"]: i for i in out}
    assert by_num[42]["fields"] == {"duration_days": 5, "confidence": "high"}
    # Issue 43 isn't on the project board
    assert by_num[43]["fields"] == {}
    print("test_merge_attaches_fields_by_issue_number: OK")


def test_merge_skips_pr_items() -> None:
    _reset()
    issues = [{"number": 1, "title": "issue"}]
    items = [
        {
            "id": "PVTI_pr",
            "type": "PullRequest",
            "content": {"type": "PullRequest", "number": 1},
            "duration_days": 99,
        },
        {
            "id": "PVTI_iss",
            "type": "Issue",
            "content": {"type": "Issue", "number": 1},
            "duration_days": 7,
        },
    ]
    out = si.merge_issues_with_fields(issues, items)
    # Only the Issue item's fields should be attached
    assert out[0]["fields"]["duration_days"] == 7
    print("test_merge_skips_pr_items: OK")


def test_merge_strips_reserved_metadata_keys() -> None:
    """`id`, `title`, `type`, `content`, and `status` are item metadata,
    not custom field values, and shouldn't end up in `fields`."""
    _reset()
    items = [
        {
            "id": "PVTI_x",
            "title": "stolen",
            "type": "Issue",
            "status": "Todo",
            "content": {"type": "Issue", "number": 42},
            "duration_days": 5,
        }
    ]
    issues = [{"number": 42}]
    out = si.merge_issues_with_fields(issues, items)
    fields = out[0]["fields"]
    assert "id" not in fields
    assert "title" not in fields
    assert "type" not in fields
    assert "content" not in fields
    assert "status" not in fields
    assert fields == {"duration_days": 5}
    print("test_merge_strips_reserved_metadata_keys: OK")


def test_merge_handles_no_project_items_for_issue() -> None:
    _reset()
    issues = [{"number": 1}]
    out = si.merge_issues_with_fields(issues, [])
    assert out[0]["fields"] == {}
    print("test_merge_handles_no_project_items_for_issue: OK")


# ---------- orchestration: sync() ----------


def test_sync_full_flow_with_project() -> None:
    _reset()
    _write_config(
        {
            "repo": "KKallas/Imp",
            "project_number": 7,
            "project_owner": "KKallas",
        }
    )
    issues_payload = json.dumps(
        [{"number": 42, "title": "A", "state": "OPEN", "labels": []}]
    )
    items_payload = json.dumps(
        {
            "items": [
                {
                    "id": "PVTI_1",
                    "type": "Issue",
                    "content": {"type": "Issue", "number": 42},
                    "duration_days": 3,
                }
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "issue", "list"], 0, issues_payload, ""),
            (["gh", "project", "item-list"], 0, items_payload, ""),
        ]
    )
    si.run_gh = fake

    payload = si.sync()

    assert payload["repo"] == "KKallas/Imp"
    assert payload["project_number"] == 7
    assert payload["project_owner"] == "KKallas"
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["fields"]["duration_days"] == 3
    # ISO timestamp present
    assert "T" in payload["synced_at"]
    print("test_sync_full_flow_with_project: OK")


def test_sync_skips_project_when_not_configured() -> None:
    _reset()
    _write_config({"repo": "KKallas/Imp"})  # no project_number
    issues_payload = json.dumps(
        [{"number": 1, "title": "A", "state": "OPEN", "labels": []}]
    )
    fake = FakeGh([(["gh", "issue", "list"], 0, issues_payload, "")])
    si.run_gh = fake

    payload = si.sync()

    assert payload["project_number"] is None
    assert payload["issues"][0]["fields"] == {}
    # No project item-list call was made
    assert not any("item-list" in c for c in fake.calls)
    print("test_sync_skips_project_when_not_configured: OK")


def test_sync_falls_back_to_owner_from_repo() -> None:
    """If project_owner isn't set explicitly but project_number is,
    derive owner from `repo`."""
    _reset()
    _write_config({"repo": "KKallas/Imp", "project_number": 7})  # no owner
    issues_payload = json.dumps([])
    items_payload = json.dumps({"items": []})
    fake = FakeGh(
        [
            (["gh", "issue", "list"], 0, issues_payload, ""),
            (["gh", "project", "item-list"], 0, items_payload, ""),
        ]
    )
    si.run_gh = fake
    payload = si.sync()
    assert payload["project_owner"] == "KKallas"
    # Verify the item-list call used the derived owner
    item_call = next(c for c in fake.calls if "item-list" in c)
    assert "KKallas" in item_call
    print("test_sync_falls_back_to_owner_from_repo: OK")


def test_sync_errors_when_no_repo_in_config() -> None:
    _reset()
    _write_config({})  # empty config
    fake = FakeGh([])
    si.run_gh = fake
    try:
        si.sync()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "repo" in str(exc).lower()
    print("test_sync_errors_when_no_repo_in_config: OK")


def test_write_output_creates_imp_dir_and_writes_json() -> None:
    _reset()
    payload = {
        "synced_at": "2026-04-15T06:30:00+00:00",
        "repo": "KKallas/Imp",
        "issue_count": 0,
        "issues": [],
    }
    si.write_output(payload, path=si.OUTPUT_FILE)
    assert si.OUTPUT_FILE.exists()
    on_disk = json.loads(si.OUTPUT_FILE.read_text())
    assert on_disk == payload
    print("test_write_output_creates_imp_dir_and_writes_json: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_normalize_field_value_passes_scalars,
        test_normalize_field_value_unwraps_dict_shapes,
        test_owner_from_repo,
        test_fetch_issues_builds_argv_and_parses_json,
        test_fetch_issues_surfaces_gh_error,
        test_fetch_project_items_parses_items_list,
        test_merge_attaches_fields_by_issue_number,
        test_merge_skips_pr_items,
        test_merge_strips_reserved_metadata_keys,
        test_merge_handles_no_project_items_for_issue,
        test_sync_full_flow_with_project,
        test_sync_skips_project_when_not_configured,
        test_sync_falls_back_to_owner_from_repo,
        test_sync_errors_when_no_repo_in_config,
        test_write_output_creates_imp_dir_and_writes_json,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} sync_issues tests passed.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
