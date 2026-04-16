"""Tests for server/foreman_agent.py.

Run directly: `.venv/bin/python tests/test_foreman_agent.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Targets the `do_*` tool-body coroutines. Every body ultimately calls
`intercept.execute_command` (except the pure-config ones like
`do_loop_pause`), so the tests monkey-patch that function with a
scripted fake. No real subprocesses, no gh binary, no Claude SDK
dependency.

`CONFIG_FILE` is redirected to a tempdir so the shared
`.imp/config.json` is never touched.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import foreman_agent  # noqa: E402

_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-foreman-test-"))
foreman_agent.CONFIG_FILE = _TMP_DIR / "config.json"


# ---------- fake intercept ----------


@dataclass
class FakeAction:
    """Stands in for intercept.ProposedAction — only the fields the
    foreman result builder reads."""

    action_id: str = "act_test"
    verdict: str = "approve"
    verdict_reason: str | None = None
    classified_as: str = "read"
    returncode: int | None = 0


class FakeIntercept:
    """Scripted replacement for `intercept.execute_command`.

    Pushes each scripted response in order; records every call's argv,
    user_intent, rationale, and kind.
    """

    def __init__(self) -> None:
        self.responses: list[tuple[int, str, FakeAction]] = []
        self.calls: list[dict[str, Any]] = []

    def script(self, responses: list[tuple[int, str, FakeAction]]) -> None:
        self.responses = list(responses)

    async def __call__(
        self,
        argv: list[str],
        *,
        user_intent: str = "",
        rationale: str = "",
        kind: str = "run",
        step: Any = None,
        task_entry: Any = None,
    ) -> tuple[int, str, FakeAction]:
        self.calls.append(
            {
                "argv": list(argv),
                "user_intent": user_intent,
                "rationale": rationale,
                "kind": kind,
            }
        )
        if not self.responses:
            raise AssertionError(
                f"FakeIntercept ran out of responses; argv={argv!r}"
            )
        return self.responses.pop(0)


_FAKE = FakeIntercept()
# Install the patch at import time — every do_* call in these tests
# hits FAKE instead of the real intercept.execute_command.
foreman_agent.intercept.execute_command = _FAKE


def _reset() -> None:
    _FAKE.responses.clear()
    _FAKE.calls.clear()
    if foreman_agent.CONFIG_FILE.exists():
        foreman_agent.CONFIG_FILE.unlink()


# ---------- read / visibility ----------


async def test_do_list_issues_builds_argv_and_passes_intent() -> None:
    _reset()
    _FAKE.script([(0, "#42 OPEN [P4.11]", FakeAction(classified_as="read"))])
    res = await foreman_agent.do_list_issues(
        state="open", limit=25, user_intent="admin asked for issues"
    )
    assert res["exit_code"] == 0
    assert "#42" in res["output"]
    assert res["verdict"] == "approve"
    call = _FAKE.calls[0]
    assert call["argv"] == ["gh", "issue", "list", "--state", "open", "--limit", "25"]
    assert call["user_intent"] == "admin asked for issues"
    assert "list issues" in call["rationale"]
    assert call["kind"] == "foreman"
    print("test_do_list_issues_builds_argv_and_passes_intent: OK")


async def test_do_view_issue_builds_argv() -> None:
    _reset()
    _FAKE.script([(0, "issue body", FakeAction(classified_as="read"))])
    await foreman_agent.do_view_issue(42, user_intent="view 42")
    assert _FAKE.calls[0]["argv"] == ["gh", "issue", "view", "42"]
    print("test_do_view_issue_builds_argv: OK")


async def test_do_list_prs_builds_argv() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="read"))])
    await foreman_agent.do_list_prs(state="closed", limit=5, user_intent="u")
    assert _FAKE.calls[0]["argv"] == ["gh", "pr", "list", "--state", "closed", "--limit", "5"]
    print("test_do_list_prs_builds_argv: OK")


async def test_do_view_pr_builds_argv() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="read"))])
    await foreman_agent.do_view_pr(17, user_intent="u")
    assert _FAKE.calls[0]["argv"] == ["gh", "pr", "view", "17"]
    print("test_do_view_pr_builds_argv: OK")


async def test_do_list_project_items_builds_argv() -> None:
    _reset()
    _FAKE.script([(0, "[]", FakeAction(classified_as="read"))])
    await foreman_agent.do_list_project_items(
        project_number=7, owner="KKallas", limit=200, user_intent="u"
    )
    call = _FAKE.calls[0]
    assert "item-list" in call["argv"]
    assert "7" in call["argv"]
    assert "--owner" in call["argv"]
    assert "KKallas" in call["argv"]
    assert "200" in call["argv"]
    print("test_do_list_project_items_builds_argv: OK")


# ---------- PM writes ----------


async def test_do_comment_on_issue() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_comment_on_issue(42, "hello", user_intent="u")
    assert _FAKE.calls[0]["argv"] == [
        "gh",
        "issue",
        "comment",
        "42",
        "--body",
        "hello",
    ]
    print("test_do_comment_on_issue: OK")


async def test_do_edit_issue_labels_and_title() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_edit_issue(
        42,
        add_labels=["llm-ready", "foo"],
        remove_labels=["stale"],
        title="new title",
        user_intent="u",
    )
    argv = _FAKE.calls[0]["argv"]
    assert argv[:4] == ["gh", "issue", "edit", "42"]
    # Each --add-label gets its own flag
    assert argv.count("--add-label") == 2
    assert "llm-ready" in argv and "foo" in argv
    assert argv.count("--remove-label") == 1
    assert "stale" in argv
    assert "--title" in argv
    assert "new title" in argv
    print("test_do_edit_issue_labels_and_title: OK")


async def test_do_edit_issue_no_fields_errors() -> None:
    """edit_issue with no fields to change returns an error without
    shelling out."""
    _reset()
    _FAKE.script([])  # no response — if intercept is called, test fails
    res = await foreman_agent.do_edit_issue(42, user_intent="u")
    assert "error" in res, res
    assert _FAKE.calls == []
    print("test_do_edit_issue_no_fields_errors: OK")


async def test_do_close_issue_with_reason() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_close_issue(
        42, reason="completed", comment="ship it", user_intent="u"
    )
    argv = _FAKE.calls[0]["argv"]
    assert argv == [
        "gh",
        "issue",
        "close",
        "42",
        "--reason",
        "completed",
        "--comment",
        "ship it",
    ]
    print("test_do_close_issue_with_reason: OK")


async def test_do_reopen_issue() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_reopen_issue(42, user_intent="u")
    assert _FAKE.calls[0]["argv"] == ["gh", "issue", "reopen", "42"]
    print("test_do_reopen_issue: OK")


async def test_do_create_issue_with_labels() -> None:
    _reset()
    _FAKE.script([(0, "https://github.com/o/r/issues/99", FakeAction(classified_as="write"))])
    await foreman_agent.do_create_issue(
        title="hello",
        body="world",
        labels=["bug", "p0"],
        user_intent="u",
    )
    argv = _FAKE.calls[0]["argv"]
    assert argv[:4] == ["gh", "issue", "create", "--title"]
    assert "hello" in argv and "world" in argv
    assert argv.count("--label") == 2
    print("test_do_create_issue_with_labels: OK")


async def test_do_edit_project_field() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_edit_project_field(
        project_number=7,
        owner="KKallas",
        item_id="PVTI_abc",
        field_id="PVTF_xyz",
        value="high",
        user_intent="u",
    )
    argv = _FAKE.calls[0]["argv"]
    assert "item-edit" in argv
    assert "PVTI_abc" in argv
    assert "PVTF_xyz" in argv
    assert "high" in argv
    print("test_do_edit_project_field: OK")


# ---------- code-writing pipeline ----------


async def test_do_run_moderate_issues() -> None:
    _reset()
    _FAKE.script([(0, "moderated", FakeAction(classified_as="write"))])
    await foreman_agent.do_run_moderate_issues(
        issue=42, max_tokens=5000, user_intent="u"
    )
    argv = _FAKE.calls[0]["argv"]
    assert argv[1] == "99-tools/moderate_issues.py"
    assert "--issue" in argv
    assert "42" in argv
    assert "--max-tokens" in argv
    assert "5000" in argv
    print("test_do_run_moderate_issues: OK")


async def test_do_run_solve_issues() -> None:
    _reset()
    _FAKE.script([(0, "solved", FakeAction(classified_as="write"))])
    await foreman_agent.do_run_solve_issues(issue=7, user_intent="u")
    argv = _FAKE.calls[0]["argv"]
    assert argv[1] == "99-tools/solve_issues.py"
    assert "--issue" in argv and "7" in argv
    print("test_do_run_solve_issues: OK")


async def test_do_run_fix_prs() -> None:
    _reset()
    _FAKE.script([(0, "", FakeAction(classified_as="write"))])
    await foreman_agent.do_run_fix_prs(pr=17, user_intent="u")
    argv = _FAKE.calls[0]["argv"]
    assert argv[1] == "99-tools/fix_prs.py"
    assert "--pr" in argv and "17" in argv
    print("test_do_run_fix_prs: OK")


# ---------- pipeline: visibility scripts (stubs) ----------


async def test_do_run_render_chart_builds_argv() -> None:
    """run_render_chart forwards --template correctly. The script itself
    doesn't exist yet (P4.14), but the wiring must be right."""
    _reset()
    _FAKE.script([(0, "<html>", FakeAction(classified_as="read"))])
    await foreman_agent.do_run_render_chart(template="gantt", user_intent="u")
    argv = _FAKE.calls[0]["argv"]
    assert argv[1] == "pipeline/render_chart.py"
    assert "--template" in argv
    assert "gantt" in argv
    print("test_do_run_render_chart_builds_argv: OK")


# ---------- control tools (no intercept, pure config) ----------


async def test_do_loop_pause_and_resume() -> None:
    _reset()
    res = await foreman_agent.do_loop_pause()
    assert res["paused"] is True
    cfg = foreman_agent._load_config()
    assert cfg["loop"]["paused"] is True
    res = await foreman_agent.do_loop_resume()
    assert res["paused"] is False
    assert foreman_agent._load_config()["loop"]["paused"] is False
    print("test_do_loop_pause_and_resume: OK")


async def test_do_loop_scope_accepts_only_issues() -> None:
    _reset()
    res = await foreman_agent.do_loop_scope(only_issues=[42, 43])
    assert res["scope"] == {"only_issues": [42, 43]}
    cfg = foreman_agent._load_config()
    assert cfg["loop"]["scope"] == {"only_issues": [42, 43]}
    print("test_do_loop_scope_accepts_only_issues: OK")


async def test_do_loop_scope_rejects_empty() -> None:
    _reset()
    res = await foreman_agent.do_loop_scope()
    assert "error" in res
    cfg = foreman_agent._load_config()
    # No loop block written at all
    assert "loop" not in cfg or cfg.get("loop", {}).get("scope") is None
    print("test_do_loop_scope_rejects_empty: OK")


async def test_do_loop_clear_scope() -> None:
    _reset()
    foreman_agent._save_config(
        {"loop": {"scope": {"only_issues": [1]}, "paused": False}}
    )
    res = await foreman_agent.do_loop_clear_scope()
    assert res["scope"] is None
    assert foreman_agent._load_config()["loop"]["scope"] is None
    print("test_do_loop_clear_scope: OK")


async def test_do_get_budgets_returns_dict_shape() -> None:
    _reset()
    res = await foreman_agent.do_get_budgets()
    # Shape from budgets.BudgetState.to_dict()
    assert set(res.keys()) == {"tokens", "edits", "tasks"}
    for key in ("tokens", "edits", "tasks"):
        assert set(res[key].keys()) >= {"used", "limit"}
    print("test_do_get_budgets_returns_dict_shape: OK")


# ---------- escape hatch ----------


async def test_do_run_shell_passes_argv_through() -> None:
    _reset()
    _FAKE.script([(0, "hello\n", FakeAction(classified_as="read"))])
    res = await foreman_agent.do_run_shell(
        ["echo", "hello"], user_intent="echo test", rationale="smoke"
    )
    assert _FAKE.calls[0]["argv"] == ["echo", "hello"]
    assert _FAKE.calls[0]["rationale"] == "smoke"
    assert res["exit_code"] == 0
    assert "hello" in res["output"]
    print("test_do_run_shell_passes_argv_through: OK")


# ---------- output truncation ----------


async def test_shell_result_truncates_oversize_output() -> None:
    """_shell_result caps stdout at 8k chars so a chatty subprocess
    can't blow out the LLM's context."""
    _reset()
    big = "x" * 20_000
    res = foreman_agent._shell_result(0, big, FakeAction())
    assert len(res["output"]) < 9000
    assert "truncated" in res["output"].lower()
    print("test_shell_result_truncates_oversize_output: OK")


# ---------- rejection propagation ----------


async def test_rejection_surfaces_verdict_and_reason() -> None:
    """Budget / guard rejections come back via action.verdict_reason."""
    _reset()
    _FAKE.script(
        [
            (
                1,
                "",
                FakeAction(
                    verdict="reject",
                    verdict_reason="edits budget exhausted",
                    classified_as="write",
                ),
            )
        ]
    )
    res = await foreman_agent.do_comment_on_issue(42, "hello", user_intent="u")
    assert res["verdict"] == "reject"
    assert "edits" in res["verdict_reason"]
    assert res["exit_code"] == 1
    print("test_rejection_surfaces_verdict_and_reason: OK")


# ---------- runner ----------


async def amain() -> None:
    tests = [
        test_do_list_issues_builds_argv_and_passes_intent,
        test_do_view_issue_builds_argv,
        test_do_list_prs_builds_argv,
        test_do_view_pr_builds_argv,
        test_do_list_project_items_builds_argv,
        test_do_comment_on_issue,
        test_do_edit_issue_labels_and_title,
        test_do_edit_issue_no_fields_errors,
        test_do_close_issue_with_reason,
        test_do_reopen_issue,
        test_do_create_issue_with_labels,
        test_do_edit_project_field,
        test_do_run_moderate_issues,
        test_do_run_solve_issues,
        test_do_run_fix_prs,
        test_do_run_render_chart_builds_argv,
        test_do_loop_pause_and_resume,
        test_do_loop_scope_accepts_only_issues,
        test_do_loop_scope_rejects_empty,
        test_do_loop_clear_scope,
        test_do_get_budgets_returns_dict_shape,
        test_do_run_shell_passes_argv_through,
        test_shell_result_truncates_oversize_output,
        test_rejection_surfaces_verdict_and_reason,
    ]
    for t in tests:
        await t()
    print(f"\nAll {len(tests)} foreman-agent tests passed.")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback

        print(f"\nERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
