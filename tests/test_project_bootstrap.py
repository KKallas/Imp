"""Tests for pipeline/project_bootstrap.py.

Run directly: `.venv/bin/python tests/test_project_bootstrap.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Covers the bootstrap_project() orchestrator and its helpers by monkey-
patching `project_bootstrap.run_gh` with a scripted fake. Never shells
out to a real `gh` binary, so the tests work in CI and on a fresh
checkout with no GitHub auth.

CONFIG_FILE is redirected to a tempdir so the shared `.imp/config.json`
never gets clobbered.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import project_bootstrap as pb  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-pb-test-"))
pb.CONFIG_FILE = _TMP_DIR / "config.json"


# ---------- fake gh runner ----------


class FakeGh:
    """Scripted `run_gh` double.

    `responses` is a list of (argv_matcher, rc, stdout) triples. Each
    call pops the first matcher that hits. `argv_matcher` is a sequence
    whose elements must appear in order inside argv — extras in argv are
    allowed. None in `argv_matcher` matches any token.
    """

    def __init__(self, responses: list[tuple[list[str], int, str]]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def _matches(self, argv: list[str], pattern: list[str]) -> bool:
        """pattern tokens must appear in argv in order (possibly with gaps)."""
        i = 0
        for tok in argv:
            if i >= len(pattern):
                return True
            if pattern[i] is None or pattern[i] == tok:
                i += 1
        return i >= len(pattern)

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        for idx, (pattern, rc, out) in enumerate(self.responses):
            if self._matches(argv, pattern):
                self.responses.pop(idx)
                return (rc, out)
        raise AssertionError(
            f"FakeGh had no scripted response for argv={argv!r}; "
            f"remaining patterns={[p for p, _, _ in self.responses]!r}"
        )


def _reset_config() -> None:
    if pb.CONFIG_FILE.exists():
        pb.CONFIG_FILE.unlink()


# ---------- test cases ----------


def test_fields_template_has_all_seven() -> None:
    """Sanity check: templates/fields.json declares the 7 fields from v0.1.md."""
    _reset_config()
    fields = pb.load_fields_template()
    names = [f["name"] for f in fields]
    expected = [
        "duration_days",
        "start_date",
        "end_date",
        "confidence",
        "source",
        "assignee_verified",
        "depends_on",
    ]
    assert names == expected, names
    # Single-select fields must declare options
    by_name = {f["name"]: f for f in fields}
    assert by_name["confidence"]["options"] == ["high", "medium", "low"]
    assert by_name["source"]["options"] == ["github", "heuristic", "llm"]
    assert by_name["assignee_verified"]["options"] == ["yes", "no"]
    # Scalar fields have no options key
    assert "options" not in by_name["duration_days"]
    assert "options" not in by_name["depends_on"]
    print("test_fields_template_has_all_seven: OK")


def test_bootstrap_creates_new_project_and_all_fields() -> None:
    """No existing project → create one and all 7 fields; persist to config."""
    _reset_config()
    empty_list = json.dumps({"projects": []})
    created_project = json.dumps({"number": 7, "title": "Imp", "url": "https://..."})
    empty_fields = json.dumps({"fields": []})

    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, empty_list),
            (["gh", "project", "create"], 0, created_project),
            (["gh", "project", "field-list"], 0, empty_fields),
            # 7 field-creates follow, in template order
            (["gh", "project", "field-create", None, None, None, None, "duration_days"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "start_date"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "end_date"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "confidence"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "source"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "assignee_verified"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "depends_on"], 0, "{}"),
        ]
    )
    pb.run_gh = fake

    result = pb.bootstrap_project(owner="KKallas", title="Imp")

    assert result["project_number"] == 7
    assert result["project_owner"] == "KKallas"
    assert result["project_status"] == "created"
    assert result["created_fields"] == [
        "duration_days",
        "start_date",
        "end_date",
        "confidence",
        "source",
        "assignee_verified",
        "depends_on",
    ]
    assert result["skipped_fields"] == []

    # Config written
    cfg = json.loads(pb.CONFIG_FILE.read_text())
    assert cfg["project_number"] == 7
    assert cfg["project_owner"] == "KKallas"

    # Verify SINGLE_SELECT fields received their options in argv
    for call in fake.calls:
        if "field-create" in call and "confidence" in call:
            assert "--single-select-options" in call
            idx = call.index("--single-select-options")
            assert call[idx + 1] == "high,medium,low"
    print("test_bootstrap_creates_new_project_and_all_fields: OK")


def test_bootstrap_reuses_existing_project_idempotent() -> None:
    """Existing project with ALL fields → no creation calls, config still written."""
    _reset_config()
    existing_project_list = json.dumps(
        {"projects": [{"number": 42, "title": "Imp", "url": "..."}]}
    )
    all_fields = json.dumps(
        {
            "fields": [
                {"name": "Status", "dataType": "SINGLE_SELECT"},  # default field
                {"name": "duration_days", "dataType": "NUMBER"},
                {"name": "start_date", "dataType": "DATE"},
                {"name": "end_date", "dataType": "DATE"},
                {"name": "confidence", "dataType": "SINGLE_SELECT"},
                {"name": "source", "dataType": "SINGLE_SELECT"},
                {"name": "assignee_verified", "dataType": "SINGLE_SELECT"},
                {"name": "depends_on", "dataType": "TEXT"},
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, existing_project_list),
            (["gh", "project", "field-list"], 0, all_fields),
        ]
    )
    pb.run_gh = fake

    result = pb.bootstrap_project(owner="KKallas", title="Imp")

    assert result["project_number"] == 42
    assert result["project_status"] == "existing"
    assert result["created_fields"] == [], result["created_fields"]
    assert sorted(result["skipped_fields"]) == sorted(
        [
            "duration_days",
            "start_date",
            "end_date",
            "confidence",
            "source",
            "assignee_verified",
            "depends_on",
        ]
    )
    # No field-create calls fired
    assert not any("field-create" in c for c in fake.calls)

    cfg = json.loads(pb.CONFIG_FILE.read_text())
    assert cfg["project_number"] == 42
    print("test_bootstrap_reuses_existing_project_idempotent: OK")


def test_bootstrap_partial_existing_fields_creates_only_missing() -> None:
    """Existing project with some fields already → only create the gaps."""
    _reset_config()
    project_list = json.dumps(
        {"projects": [{"number": 3, "title": "Imp"}]}
    )
    half_fields = json.dumps(
        {
            "fields": [
                {"name": "Status"},
                {"name": "duration_days"},
                {"name": "start_date"},
                {"name": "confidence"},
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, project_list),
            (["gh", "project", "field-list"], 0, half_fields),
            # The 4 missing fields: end_date, source, assignee_verified, depends_on
            (["gh", "project", "field-create", None, None, None, None, "end_date"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "source"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "assignee_verified"], 0, "{}"),
            (["gh", "project", "field-create", None, None, None, None, "depends_on"], 0, "{}"),
        ]
    )
    pb.run_gh = fake

    result = pb.bootstrap_project(owner="KKallas", title="Imp")

    assert result["project_number"] == 3
    assert result["project_status"] == "existing"
    assert sorted(result["created_fields"]) == sorted(
        ["end_date", "source", "assignee_verified", "depends_on"]
    )
    assert sorted(result["skipped_fields"]) == sorted(
        ["duration_days", "start_date", "confidence"]
    )
    print("test_bootstrap_partial_existing_fields_creates_only_missing: OK")


def test_bootstrap_aborts_without_writing_config_on_create_failure() -> None:
    """If field-create fails, config must not be written — so a re-run can
    pick up where we stopped."""
    _reset_config()
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, json.dumps({"projects": []})),
            (["gh", "project", "create"], 0, json.dumps({"number": 9, "title": "Imp"})),
            (["gh", "project", "field-list"], 0, json.dumps({"fields": []})),
            # First field-create succeeds, second fails
            (["gh", "project", "field-create", None, None, None, None, "duration_days"], 0, "{}"),
            (
                ["gh", "project", "field-create", None, None, None, None, "start_date"],
                1,
                "scope 'project' missing",
            ),
        ]
    )
    pb.run_gh = fake

    try:
        pb.bootstrap_project(owner="KKallas", title="Imp")
        assert False, "expected RuntimeError on field-create failure"
    except RuntimeError as exc:
        assert "start_date" in str(exc)
        assert "scope" in str(exc)

    # Config must NOT have been written — we aborted mid-way
    assert not pb.CONFIG_FILE.exists() or "project_number" not in json.loads(
        pb.CONFIG_FILE.read_text()
    )
    print("test_bootstrap_aborts_without_writing_config_on_create_failure: OK")


def test_bootstrap_errors_on_non_integer_project_number() -> None:
    """If gh returns a malformed project payload, we fail loud."""
    _reset_config()
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, json.dumps({"projects": []})),
            (
                ["gh", "project", "create"],
                0,
                json.dumps({"number": "not-an-int", "title": "Imp"}),
            ),
        ]
    )
    pb.run_gh = fake

    try:
        pb.bootstrap_project(owner="KKallas", title="Imp")
        assert False, "expected RuntimeError on non-integer project number"
    except RuntimeError as exc:
        assert "integer" in str(exc).lower()
    print("test_bootstrap_errors_on_non_integer_project_number: OK")


def test_list_fails_surface_gh_error() -> None:
    """gh errors on project list → RuntimeError with gh's text in the message."""
    _reset_config()
    fake = FakeGh(
        [
            (["gh", "project", "list"], 1, "HTTP 401: Bad credentials"),
        ]
    )
    pb.run_gh = fake

    try:
        pb.bootstrap_project(owner="KKallas", title="Imp")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "project list" in str(exc)
        assert "401" in str(exc)
    print("test_list_fails_surface_gh_error: OK")


def test_single_select_requires_options() -> None:
    """A SINGLE_SELECT field def without options list must raise before hitting gh."""
    _reset_config()
    fake = FakeGh([])
    pb.run_gh = fake
    try:
        pb.create_field(
            "KKallas",
            7,
            {"name": "bad_field", "type": "SINGLE_SELECT", "options": []},
        )
        assert False, "expected ValueError on empty options"
    except ValueError as exc:
        assert "options" in str(exc)
    # No gh call was made — validation fires before the subprocess
    assert fake.calls == []
    print("test_single_select_requires_options: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_fields_template_has_all_seven,
        test_bootstrap_creates_new_project_and_all_fields,
        test_bootstrap_reuses_existing_project_idempotent,
        test_bootstrap_partial_existing_fields_creates_only_missing,
        test_bootstrap_aborts_without_writing_config_on_create_failure,
        test_bootstrap_errors_on_non_integer_project_number,
        test_list_fails_surface_gh_error,
        test_single_select_requires_options,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} project_bootstrap tests passed.")


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
