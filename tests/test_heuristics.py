"""Tests for pipeline/heuristics.py.

Run directly: `.venv/bin/python tests/test_heuristics.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Targets each helper in isolation plus the full `enrich()` flow against
the canonical fixture at `tests/fixtures/sample_issues.json`. No
network / no gh dependency — heuristics is pure data transform.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import heuristics as h  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_issues.json"

# Pin "today" so the delay tests are deterministic. Fixture issue #12
# has end_date=2026-04-13 and is OPEN with imp:baseline → delayed by
# 2 days against this anchor.
TODAY = date(2026, 4, 15)


# ---------- parse_depends_on ----------


def test_parse_depends_on_basic_hash_csv() -> None:
    parsed, bad = h.parse_depends_on("#12, #15")
    assert parsed == [12, 15]
    assert bad == []
    print("test_parse_depends_on_basic_hash_csv: OK")


def test_parse_depends_on_strips_hash_and_whitespace() -> None:
    parsed, bad = h.parse_depends_on("  12 ,  #99  ,  3 ")
    assert parsed == [12, 99, 3]
    assert bad == []
    print("test_parse_depends_on_strips_hash_and_whitespace: OK")


def test_parse_depends_on_empty_or_none() -> None:
    assert h.parse_depends_on("") == ([], [])
    assert h.parse_depends_on("   ") == ([], [])
    # Defensive against non-string inputs the caller might forward
    assert h.parse_depends_on(None) == ([], [])  # type: ignore[arg-type]
    print("test_parse_depends_on_empty_or_none: OK")


def test_parse_depends_on_collects_unparseable_without_aborting() -> None:
    """Per AC: unparseable values are logged but do NOT abort."""
    parsed, bad = h.parse_depends_on("#12, garbage, TBD-43, 99, #not-a-num, 5")
    assert parsed == [12, 99, 5]
    assert sorted(bad) == sorted(["garbage", "TBD-43", "#not-a-num"])
    print("test_parse_depends_on_collects_unparseable_without_aborting: OK")


# ---------- infer_duration ----------


def test_infer_duration_uses_github_value_when_present() -> None:
    issue = {"fields": {"duration_days": 7}, "labels": []}
    days, source, conf = h.infer_duration(issue)
    assert days == 7
    assert source == "github"
    assert conf == "high"
    print("test_infer_duration_uses_github_value_when_present: OK")


def test_infer_duration_uses_label_hint_when_field_missing() -> None:
    issue = {"fields": {}, "labels": [{"name": "area:server"}]}
    days, source, conf = h.infer_duration(issue)
    assert days == h.DURATION_HINT_BY_LABEL["area:server"]
    assert source == "heuristic"
    assert conf == "medium"
    print("test_infer_duration_uses_label_hint_when_field_missing: OK")


def test_infer_duration_falls_back_to_default() -> None:
    issue = {"fields": {}, "labels": [{"name": "unknown"}]}
    days, source, conf = h.infer_duration(issue)
    assert days == h.DEFAULT_DURATION_DAYS
    assert source == "heuristic"
    assert conf == "low"
    print("test_infer_duration_falls_back_to_default: OK")


def test_infer_duration_handles_zero_or_negative_as_missing() -> None:
    """A 0 or negative duration_days shouldn't be trusted."""
    issue = {"fields": {"duration_days": 0}, "labels": [{"name": "area:server"}]}
    days, source, _ = h.infer_duration(issue)
    assert source == "heuristic"
    assert days == h.DURATION_HINT_BY_LABEL["area:server"]
    print("test_infer_duration_handles_zero_or_negative_as_missing: OK")


# ---------- detect_delay ----------


def test_detect_delay_returns_record_for_overdue_open_baseline_issue() -> None:
    issue = {
        "state": "OPEN",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "2026-04-13"},
    }
    delay = h.detect_delay(issue, today=TODAY)
    assert delay is not None
    assert delay["is_delayed"] is True
    assert delay["days_overdue"] == 2
    assert delay["source"] == "heuristic"
    assert "passed" in delay["reason"].lower()
    print("test_detect_delay_returns_record_for_overdue_open_baseline_issue: OK")


def test_detect_delay_skips_closed_issue_even_if_overdue() -> None:
    issue = {
        "state": "CLOSED",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "2026-04-05"},
    }
    assert h.detect_delay(issue, today=TODAY) is None
    print("test_detect_delay_skips_closed_issue_even_if_overdue: OK")


def test_detect_delay_skips_issue_without_baseline_label() -> None:
    issue = {
        "state": "OPEN",
        "labels": [{"name": "area:pipeline"}],
        "fields": {"end_date": "2026-04-05"},
    }
    assert h.detect_delay(issue, today=TODAY) is None
    print("test_detect_delay_skips_issue_without_baseline_label: OK")


def test_detect_delay_skips_issue_without_end_date() -> None:
    issue = {
        "state": "OPEN",
        "labels": [{"name": "imp:baseline"}],
        "fields": {},
    }
    assert h.detect_delay(issue, today=TODAY) is None
    print("test_detect_delay_skips_issue_without_end_date: OK")


def test_detect_delay_skips_when_end_date_in_future() -> None:
    issue = {
        "state": "OPEN",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "2026-12-01"},
    }
    assert h.detect_delay(issue, today=TODAY) is None
    print("test_detect_delay_skips_when_end_date_in_future: OK")


def test_detect_delay_soft_skip_on_malformed_date() -> None:
    issue = {
        "state": "OPEN",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "not-a-date"},
    }
    assert h.detect_delay(issue, today=TODAY) is None
    print("test_detect_delay_soft_skip_on_malformed_date: OK")


# ---------- enrich (single issue + full payload) ----------


def test_enrich_issue_wraps_existing_fields_with_provenance() -> None:
    issue = {
        "number": 11,
        "labels": [{"name": "area:server"}],
        "state": "CLOSED",
        "fields": {"duration_days": 4, "confidence": "high"},
    }
    out = h.enrich_issue(issue, today=TODAY)
    fields = out["fields"]
    # Existing values get the github envelope
    assert fields["duration_days"]["source"] == "github"
    assert fields["duration_days"]["value"] == 4
    assert fields["confidence"]["source"] == "github"
    assert fields["confidence"]["value"] == "high"
    print("test_enrich_issue_wraps_existing_fields_with_provenance: OK")


def test_enrich_issue_attaches_depends_on_parsed_array() -> None:
    issue = {
        "number": 14,
        "labels": [{"name": "imp:baseline"}],
        "state": "OPEN",
        "fields": {"depends_on": "#12, garbage, #99"},
    }
    out = h.enrich_issue(issue, today=TODAY)
    assert out["depends_on_parsed"] == [12, 99]
    assert out["depends_on_unparseable"] == ["garbage"]
    deps_field = out["fields"]["depends_on"]
    assert deps_field["value"] == [12, 99]
    assert deps_field["raw"] == "#12, garbage, #99"
    assert deps_field["unparseable"] == ["garbage"]
    # Confidence drops to medium when there were parse failures
    assert deps_field["confidence"] == "medium"
    print("test_enrich_issue_attaches_depends_on_parsed_array: OK")


def test_enrich_issue_attaches_delay_record_when_overdue() -> None:
    issue = {
        "number": 12,
        "state": "OPEN",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "2026-04-13"},
    }
    out = h.enrich_issue(issue, today=TODAY)
    assert "delay" in out
    assert out["delay"]["days_overdue"] == 2
    print("test_enrich_issue_attaches_delay_record_when_overdue: OK")


def test_enrich_issue_no_delay_key_when_not_delayed() -> None:
    issue = {
        "number": 11,
        "state": "CLOSED",
        "labels": [{"name": "imp:baseline"}],
        "fields": {"end_date": "2026-04-15"},
    }
    out = h.enrich_issue(issue, today=TODAY)
    assert "delay" not in out
    print("test_enrich_issue_no_delay_key_when_not_delayed: OK")


def test_enrich_issue_does_not_mutate_input() -> None:
    issue = {
        "number": 1,
        "state": "OPEN",
        "labels": [],
        "fields": {"duration_days": 5},
    }
    snapshot = json.dumps(issue, sort_keys=True)
    h.enrich_issue(issue, today=TODAY)
    assert json.dumps(issue, sort_keys=True) == snapshot
    print("test_enrich_issue_does_not_mutate_input: OK")


def test_enrich_full_payload_against_fixture() -> None:
    """End-to-end: fixture in → enriched payload out, with the right
    counts and shapes."""
    payload = json.loads(FIXTURE.read_text())
    enriched = h.enrich(payload, today=TODAY)

    # Top-level shape
    assert enriched["repo"] == "KKallas/Imp"
    assert enriched["issue_count"] == payload["issue_count"]
    assert "enriched_at" in enriched
    assert "T" in enriched["enriched_at"]
    # Sync timestamp preserved unchanged
    assert enriched["synced_at"] == payload["synced_at"]

    by_num = {it["number"]: it for it in enriched["issues"]}

    # #11: closed, has all fields → no delay, github source on each field
    assert "delay" not in by_num[11]
    assert by_num[11]["fields"]["duration_days"]["source"] == "github"
    assert by_num[11]["depends_on_parsed"] == [10, 9]

    # #12: OPEN, imp:baseline, end_date 2026-04-13 → delayed by 2 days
    assert "delay" in by_num[12]
    assert by_num[12]["delay"]["days_overdue"] == 2
    # No github duration → heuristic from "imp:baseline" label hint
    dur12 = by_num[12]["fields"]["duration_days"]
    assert dur12["source"] == "heuristic"
    assert dur12["value"] == h.DURATION_HINT_BY_LABEL["imp:baseline"]

    # #13: empty fields, "area:pipeline" label → heuristic medium
    dur13 = by_num[13]["fields"]["duration_days"]
    assert dur13["source"] == "heuristic"
    assert dur13["value"] == h.DURATION_HINT_BY_LABEL["area:pipeline"]
    assert dur13["confidence"] == "medium"
    # Depends_on was not present → empty parsed list, no unparseable
    assert by_num[13]["depends_on_parsed"] == []
    assert "depends_on_unparseable" not in by_num[13]

    # #14: messy depends_on → parsed contains 12 and 99 (5 was a typo,
    # the fixture doesn't have it; let's just check 12 and 99 land)
    assert 12 in by_num[14]["depends_on_parsed"]
    assert 99 in by_num[14]["depends_on_parsed"]
    assert "TBD-43" in by_num[14]["depends_on_unparseable"]
    assert "#garbage" in by_num[14]["depends_on_unparseable"]

    # #15: CLOSED past end_date → NOT delayed
    assert "delay" not in by_num[15]
    # #16: OPEN past end_date but no imp:baseline → NOT delayed
    assert "delay" not in by_num[16]

    # Top-level summary metrics
    assert enriched["delayed_count"] == 1  # only #12
    edges = enriched["dependency_edges"]
    edge_pairs = {(e["from"], e["to"]) for e in edges}
    assert (11, 10) in edge_pairs
    assert (11, 9) in edge_pairs
    assert (12, 11) in edge_pairs
    assert (14, 12) in edge_pairs

    print("test_enrich_full_payload_against_fixture: OK")


# ---------- build_dependency_edges ----------


def test_build_dependency_edges_skips_self_and_non_int() -> None:
    issues = [
        {"number": 1, "depends_on_parsed": [1, 2, 3]},  # 1→1 must be skipped
        {"number": 2, "depends_on_parsed": []},
        {"number": "not-an-int", "depends_on_parsed": [4]},  # whole issue skipped
    ]
    edges = h.build_dependency_edges(issues)
    assert {(e["from"], e["to"]) for e in edges} == {(1, 2), (1, 3)}
    print("test_build_dependency_edges_skips_self_and_non_int: OK")


# ---------- runner ----------


def main() -> None:
    tests = [
        test_parse_depends_on_basic_hash_csv,
        test_parse_depends_on_strips_hash_and_whitespace,
        test_parse_depends_on_empty_or_none,
        test_parse_depends_on_collects_unparseable_without_aborting,
        test_infer_duration_uses_github_value_when_present,
        test_infer_duration_uses_label_hint_when_field_missing,
        test_infer_duration_falls_back_to_default,
        test_infer_duration_handles_zero_or_negative_as_missing,
        test_detect_delay_returns_record_for_overdue_open_baseline_issue,
        test_detect_delay_skips_closed_issue_even_if_overdue,
        test_detect_delay_skips_issue_without_baseline_label,
        test_detect_delay_skips_issue_without_end_date,
        test_detect_delay_skips_when_end_date_in_future,
        test_detect_delay_soft_skip_on_malformed_date,
        test_enrich_issue_wraps_existing_fields_with_provenance,
        test_enrich_issue_attaches_depends_on_parsed_array,
        test_enrich_issue_attaches_delay_record_when_overdue,
        test_enrich_issue_no_delay_key_when_not_delayed,
        test_enrich_issue_does_not_mutate_input,
        test_enrich_full_payload_against_fixture,
        test_build_dependency_edges_skips_self_and_non_int,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} heuristics tests passed.")


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
