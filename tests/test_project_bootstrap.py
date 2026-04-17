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
    """Sanity check: renderers/fields.json declares the 7 fields from v0.1.md."""
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
                {
                    "name": "confidence",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "high"}, {"name": "medium"}, {"name": "low"}],
                },
                {
                    "name": "source",
                    "dataType": "SINGLE_SELECT",
                    "options": [
                        {"name": "github"},
                        {"name": "heuristic"},
                        {"name": "llm"},
                    ],
                },
                {
                    "name": "assignee_verified",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "yes"}, {"name": "no"}],
                },
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
                {"name": "duration_days", "dataType": "NUMBER"},
                {"name": "start_date", "dataType": "DATE"},
                {
                    "name": "confidence",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "high"}, {"name": "medium"}, {"name": "low"}],
                },
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


def test_detect_conflict_wrong_type() -> None:
    """A same-named field with a different dataType is a conflict."""
    _reset_config()
    existing = [
        {"id": "PVTF_a", "name": "duration_days", "dataType": "TEXT"},
        {"id": "PVTF_b", "name": "start_date", "dataType": "DATE"},
    ]
    template = pb.load_fields_template()
    conflicts = pb.detect_field_conflicts(existing, template)
    by_name = {c["name"]: c for c in conflicts}
    assert "duration_days" in by_name
    assert by_name["duration_days"]["reason"] == "wrong_type"
    assert by_name["duration_days"]["expected_type"] == "NUMBER"
    assert by_name["duration_days"]["actual_type"] == "TEXT"
    assert by_name["duration_days"]["field_id"] == "PVTF_a"
    # start_date matches → no conflict
    assert "start_date" not in by_name
    print("test_detect_conflict_wrong_type: OK")


def test_detect_conflict_wrong_options() -> None:
    """A SINGLE_SELECT with a different option set is a conflict."""
    _reset_config()
    existing = [
        {
            "id": "PVTF_c",
            "name": "confidence",
            "dataType": "SINGLE_SELECT",
            "options": [{"name": "high"}, {"name": "low"}],  # missing "medium"
        },
        {
            "id": "PVTF_s",
            "name": "source",
            "dataType": "SINGLE_SELECT",
            "options": [{"name": "github"}, {"name": "heuristic"}, {"name": "llm"}],
        },
    ]
    template = pb.load_fields_template()
    conflicts = pb.detect_field_conflicts(existing, template)
    by_name = {c["name"]: c for c in conflicts}
    assert "confidence" in by_name
    assert by_name["confidence"]["reason"] == "wrong_options"
    assert set(by_name["confidence"]["expected_options"]) == {"high", "medium", "low"}
    assert set(by_name["confidence"]["actual_options"]) == {"high", "low"}
    # source matches exactly → no conflict
    assert "source" not in by_name
    print("test_detect_conflict_wrong_options: OK")


def test_detect_conflict_missing_field_is_not_conflict() -> None:
    """A field that doesn't exist yet is missing, not a conflict."""
    _reset_config()
    template = pb.load_fields_template()
    conflicts = pb.detect_field_conflicts([], template)
    assert conflicts == []
    print("test_detect_conflict_missing_field_is_not_conflict: OK")


def test_bootstrap_on_conflict_stop_raises_and_keeps_config_clean() -> None:
    """Default stop mode: raise ConflictError, don't write config, no field writes."""
    _reset_config()
    project_list = json.dumps({"projects": [{"number": 5, "title": "Imp"}]})
    bad_fields = json.dumps(
        {
            "fields": [
                {"id": "PVTF_a", "name": "duration_days", "dataType": "TEXT"},
                {"id": "PVTF_b", "name": "confidence", "dataType": "TEXT"},
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, project_list),
            (["gh", "project", "field-list"], 0, bad_fields),
        ]
    )
    pb.run_gh = fake

    try:
        pb.bootstrap_project(owner="KKallas", title="Imp", on_conflict="stop")
        assert False, "expected ConflictError"
    except pb.ConflictError as exc:
        names = {c["name"] for c in exc.conflicts}
        assert names == {"duration_days", "confidence"}, names
        # The report is a dict the CLI layer dumps to stdout as JSON
        report = exc.report()
        assert report["status"] == "conflicts_detected"
        assert report["project_number"] == 5
        assert "delete" in (report.get("next_steps") or "").lower()

    # Config must not exist or at least must not carry the project_number —
    # stop mode bails before config write
    if pb.CONFIG_FILE.exists():
        cfg = json.loads(pb.CONFIG_FILE.read_text())
        assert "project_number" not in cfg, cfg
    # Critically, no field-create call was attempted
    assert not any("field-create" in c for c in fake.calls)
    assert not any("field-delete" in c for c in fake.calls)
    print("test_bootstrap_on_conflict_stop_raises_and_keeps_config_clean: OK")


def test_bootstrap_on_conflict_delete_overwrites_and_creates() -> None:
    """delete mode removes the conflicting field then recreates it fresh."""
    _reset_config()
    project_list = json.dumps({"projects": [{"number": 8, "title": "Imp"}]})
    # First list call: existing field has wrong type
    bad_fields = json.dumps(
        {
            "fields": [
                {"id": "PVTF_x", "name": "duration_days", "dataType": "TEXT"},
                {"id": "PVTF_start", "name": "start_date", "dataType": "DATE"},
                {"id": "PVTF_end", "name": "end_date", "dataType": "DATE"},
                {
                    "id": "PVTF_c",
                    "name": "confidence",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "high"}, {"name": "medium"}, {"name": "low"}],
                },
                {
                    "id": "PVTF_s",
                    "name": "source",
                    "dataType": "SINGLE_SELECT",
                    "options": [
                        {"name": "github"},
                        {"name": "heuristic"},
                        {"name": "llm"},
                    ],
                },
                {
                    "id": "PVTF_v",
                    "name": "assignee_verified",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "yes"}, {"name": "no"}],
                },
                {"id": "PVTF_d", "name": "depends_on", "dataType": "TEXT"},
            ]
        }
    )
    # After deletion, re-list shows duration_days gone
    refreshed_fields = json.dumps(
        {
            "fields": [
                {"id": "PVTF_start", "name": "start_date", "dataType": "DATE"},
                {"id": "PVTF_end", "name": "end_date", "dataType": "DATE"},
                {
                    "id": "PVTF_c",
                    "name": "confidence",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "high"}, {"name": "medium"}, {"name": "low"}],
                },
                {
                    "id": "PVTF_s",
                    "name": "source",
                    "dataType": "SINGLE_SELECT",
                    "options": [
                        {"name": "github"},
                        {"name": "heuristic"},
                        {"name": "llm"},
                    ],
                },
                {
                    "id": "PVTF_v",
                    "name": "assignee_verified",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "yes"}, {"name": "no"}],
                },
                {"id": "PVTF_d", "name": "depends_on", "dataType": "TEXT"},
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, project_list),
            (["gh", "project", "field-list"], 0, bad_fields),
            (["gh", "project", "field-delete", "--id", "PVTF_x"], 0, "{}"),
            (["gh", "project", "field-list"], 0, refreshed_fields),
            (["gh", "project", "field-create", None, None, None, None, "duration_days"], 0, "{}"),
        ]
    )
    pb.run_gh = fake

    result = pb.bootstrap_project(
        owner="KKallas", title="Imp", on_conflict="delete"
    )

    assert result["deleted_fields"] == ["duration_days"]
    assert result["created_fields"] == ["duration_days"]
    assert result["on_conflict"] == "delete"
    # Config IS written in delete mode after the overwrite completes
    cfg = json.loads(pb.CONFIG_FILE.read_text())
    assert cfg["project_number"] == 8
    # Verify delete was called with the right field ID
    delete_calls = [c for c in fake.calls if "field-delete" in c]
    assert len(delete_calls) == 1
    assert "PVTF_x" in delete_calls[0]
    print("test_bootstrap_on_conflict_delete_overwrites_and_creates: OK")


def test_bootstrap_on_conflict_skip_ignores_and_writes_config() -> None:
    """skip mode: surface conflicts in return dict but proceed; config written."""
    _reset_config()
    project_list = json.dumps({"projects": [{"number": 11, "title": "Imp"}]})
    bad_fields = json.dumps(
        {
            "fields": [
                {"id": "PVTF_x", "name": "duration_days", "dataType": "TEXT"},
                {"id": "PVTF_start", "name": "start_date", "dataType": "DATE"},
                {"id": "PVTF_end", "name": "end_date", "dataType": "DATE"},
                {
                    "id": "PVTF_c",
                    "name": "confidence",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "high"}, {"name": "medium"}, {"name": "low"}],
                },
                {
                    "id": "PVTF_s",
                    "name": "source",
                    "dataType": "SINGLE_SELECT",
                    "options": [
                        {"name": "github"},
                        {"name": "heuristic"},
                        {"name": "llm"},
                    ],
                },
                {
                    "id": "PVTF_v",
                    "name": "assignee_verified",
                    "dataType": "SINGLE_SELECT",
                    "options": [{"name": "yes"}, {"name": "no"}],
                },
                {"id": "PVTF_d", "name": "depends_on", "dataType": "TEXT"},
            ]
        }
    )
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, project_list),
            (["gh", "project", "field-list"], 0, bad_fields),
        ]
    )
    pb.run_gh = fake

    result = pb.bootstrap_project(
        owner="KKallas", title="Imp", on_conflict="skip"
    )

    # Nothing was deleted or created — the wrong-type field stays
    assert result["deleted_fields"] == []
    assert result["created_fields"] == []
    assert result["on_conflict"] == "skip"
    # The conflict is surfaced in the return so the admin knows
    conflict_names = {c["name"] for c in result["conflicts_ignored"]}
    assert conflict_names == {"duration_days"}
    # Config IS written — admin explicitly opted in to the runtime risk
    cfg = json.loads(pb.CONFIG_FILE.read_text())
    assert cfg["project_number"] == 11
    print("test_bootstrap_on_conflict_skip_ignores_and_writes_config: OK")


def test_bootstrap_rejects_bad_on_conflict_value() -> None:
    _reset_config()
    fake = FakeGh(
        [
            (["gh", "project", "list"], 0, json.dumps({"projects": [{"number": 1, "title": "Imp"}]})),
            (["gh", "project", "field-list"], 0, json.dumps({"fields": []})),
        ]
    )
    pb.run_gh = fake
    try:
        pb.bootstrap_project(owner="x", title="Imp", on_conflict="nope")
        assert False, "expected ValueError on bad on_conflict"
    except ValueError as exc:
        assert "on_conflict" in str(exc)
    print("test_bootstrap_rejects_bad_on_conflict_value: OK")


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
        test_detect_conflict_wrong_type,
        test_detect_conflict_wrong_options,
        test_detect_conflict_missing_field_is_not_conflict,
        test_bootstrap_on_conflict_stop_raises_and_keeps_config_clean,
        test_bootstrap_on_conflict_delete_overwrites_and_creates,
        test_bootstrap_on_conflict_skip_ignores_and_writes_config,
        test_bootstrap_rejects_bad_on_conflict_value,
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
