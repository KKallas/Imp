"""Tests for server/setup_agent.py.

Run directly: `.venv/bin/python tests/test_setup_agent.py`
No pytest. Asserts → exit 0 on success, exit 1 on failure.

Targets the `do_*` tool-body coroutines. They're the code under test;
the @tool-decorated wrappers in `_build_mcp_server` are thin JSON
adapters exercised only when the SDK is actually running.

Subprocess-touching tools (`do_gh_auth_status`, `do_list_repos`,
`do_set_repo`, `do_list_projects`, `do_create_imp_project`) are tested
via monkey-patching `setup_agent._run_subprocess` so the tests don't
depend on an actual `gh` CLI being authenticated in the test env.

`CONFIG_FILE` is redirected to a tempdir so the tests don't clobber
the real `.imp/config.json`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import setup_agent  # noqa: E402

# Redirect config file so tests don't touch the real .imp/config.json
_TMP_DIR = Path(tempfile.mkdtemp(prefix="imp-setup-test-"))
setup_agent.CONFIG_FILE = _TMP_DIR / "config.json"


def _reset_config() -> None:
    if setup_agent.CONFIG_FILE.exists():
        setup_agent.CONFIG_FILE.unlink()


# ---------- subprocess stub ----------


class FakeSubprocess:
    """Records argv, returns scripted (rc, output) tuples in order."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
        self.calls.append(list(argv))
        if not self.responses:
            raise AssertionError(f"FakeSubprocess ran out of responses; argv={argv}")
        return self.responses.pop(0)


# ---------- config-only tools ----------


async def test_do_mark_setup_complete_refuses_without_repo() -> None:
    _reset_config()
    res = await setup_agent.do_mark_setup_complete()
    assert res.get("error")
    assert "repo" in res["error"].lower()
    assert not setup_agent.is_setup_complete()
    print("test_do_mark_setup_complete_refuses_without_repo: OK")


async def test_do_mark_setup_complete_happy_path() -> None:
    _reset_config()
    setup_agent.save_config({"repo": "owner/name"})
    res = await setup_agent.do_mark_setup_complete()
    assert res == {"setup_complete": True, "repo": "owner/name"}
    assert setup_agent.is_setup_complete()
    print("test_do_mark_setup_complete_happy_path: OK")


async def test_do_configure_loop_saves_valid_settings() -> None:
    _reset_config()
    res = await setup_agent.do_configure_loop(
        enabled=True, interval_minutes=30, max_tasks_per_tick=5
    )
    assert res["saved"] is True
    cfg = setup_agent.load_config()
    assert cfg["loop"] == {
        "enabled": True,
        "interval_minutes": 30,
        "max_tasks_per_tick": 5,
        "scope": None,
        "paused": False,
    }
    print("test_do_configure_loop_saves_valid_settings: OK")


async def test_do_configure_loop_rejects_short_interval() -> None:
    _reset_config()
    res = await setup_agent.do_configure_loop(
        enabled=True, interval_minutes=1, max_tasks_per_tick=3
    )
    assert "error" in res
    assert "5" in res["error"]
    assert "loop" not in setup_agent.load_config()
    print("test_do_configure_loop_rejects_short_interval: OK")


async def test_do_configure_loop_rejects_zero_max_tasks() -> None:
    _reset_config()
    res = await setup_agent.do_configure_loop(
        enabled=True, interval_minutes=60, max_tasks_per_tick=0
    )
    assert "error" in res
    print("test_do_configure_loop_rejects_zero_max_tasks: OK")


async def test_do_set_admin_password_rejects_short() -> None:
    _reset_config()
    res = await setup_agent.do_set_admin_password("ab")
    assert "error" in res
    assert "4 character" in res["error"]
    assert "admin_password_hash" not in setup_agent.load_config()
    print("test_do_set_admin_password_rejects_short: OK")


async def test_do_set_admin_password_hashes_and_persists() -> None:
    _reset_config()
    res = await setup_agent.do_set_admin_password("correct-horse")
    assert res["saved"] is True
    cfg = setup_agent.load_config()
    h = cfg.get("admin_password_hash", "")
    assert h.startswith("$argon2"), h
    print("test_do_set_admin_password_hashes_and_persists: OK")


# ---------- instruction / read-only tools ----------


async def test_do_gh_auth_login_returns_instruction() -> None:
    _reset_config()
    res = await setup_agent.do_gh_auth_login()
    assert res["automated"] is False
    assert "gh auth login" in res["instruction"]
    print("test_do_gh_auth_login_returns_instruction: OK")


async def test_do_claude_auth_login_returns_instruction() -> None:
    _reset_config()
    res = await setup_agent.do_claude_auth_login()
    assert res["automated"] is False
    assert "ANTHROPIC_API_KEY" in res["instruction"]
    print("test_do_claude_auth_login_returns_instruction: OK")


async def test_do_claude_auth_status_reports_env_and_sdk() -> None:
    _reset_config()
    res = await setup_agent.do_claude_auth_status()
    assert "anthropic_api_key_set" in res
    assert "sdk_installed" in res
    # SDK is installed in the test env (test_dispatcher uses it)
    assert res["sdk_installed"] is True
    print("test_do_claude_auth_status_reports_env_and_sdk: OK")


# ---------- subprocess-backed tools (via FakeSubprocess) ----------


async def test_do_gh_auth_status_authenticated() -> None:
    _reset_config()
    fake = FakeSubprocess([(0, "Logged in to github.com as kaspar (oauth_token)")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_gh_auth_status()
    assert res["authenticated"] is True
    assert "Logged in" in res["output"]
    assert fake.calls == [["gh", "auth", "status"]]
    print("test_do_gh_auth_status_authenticated: OK")


async def test_do_gh_auth_status_not_authenticated() -> None:
    _reset_config()
    fake = FakeSubprocess([(1, "You are not logged into any GitHub hosts")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_gh_auth_status()
    assert res["authenticated"] is False
    assert "not logged" in res["output"].lower()
    print("test_do_gh_auth_status_not_authenticated: OK")


async def test_do_list_repos_parses_json() -> None:
    _reset_config()
    payload = json.dumps(
        [
            {"nameWithOwner": "KKallas/Imp", "description": "test", "visibility": "PUBLIC"},
            {"nameWithOwner": "KKallas/other", "description": "", "visibility": "PRIVATE"},
        ]
    )
    fake = FakeSubprocess([(0, payload)])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_list_repos(limit=50)
    assert res["count"] == 2
    assert res["repos"][0]["nameWithOwner"] == "KKallas/Imp"
    assert "--limit" in fake.calls[0]
    assert "50" in fake.calls[0]
    print("test_do_list_repos_parses_json: OK")


async def test_do_list_repos_handles_gh_error() -> None:
    _reset_config()
    fake = FakeSubprocess([(1, "gh: no token found")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_list_repos()
    assert res["repos"] == []
    assert "error" in res
    print("test_do_list_repos_handles_gh_error: OK")


async def test_do_set_repo_rejects_bad_shape() -> None:
    _reset_config()
    fake = FakeSubprocess([])  # Shouldn't touch subprocess
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_set_repo("not_a_repo")
    assert "error" in res
    assert "owner/name" in res["error"]
    assert "repo" not in setup_agent.load_config()
    print("test_do_set_repo_rejects_bad_shape: OK")


async def test_do_set_repo_verifies_then_writes() -> None:
    _reset_config()
    fake = FakeSubprocess(
        [
            (
                0,
                json.dumps(
                    {
                        "nameWithOwner": "KKallas/Imp",
                        "defaultBranchRef": {"name": "main"},
                        "visibility": "PUBLIC",
                    }
                ),
            )
        ]
    )
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_set_repo("KKallas/Imp")
    assert res["repo"] == "KKallas/Imp"
    assert res["verified"] is True
    assert setup_agent.load_config()["repo"] == "KKallas/Imp"
    print("test_do_set_repo_verifies_then_writes: OK")


async def test_do_set_repo_refuses_when_gh_rejects() -> None:
    _reset_config()
    fake = FakeSubprocess([(1, "GraphQL error: Could not resolve to a Repository")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_set_repo("ghost/repo")
    assert "error" in res
    assert "gh repo view" in res["error"]
    assert "repo" not in setup_agent.load_config()
    print("test_do_set_repo_refuses_when_gh_rejects: OK")


async def test_do_list_projects_parses_projects_object() -> None:
    _reset_config()
    payload = json.dumps(
        {"projects": [{"number": 7, "title": "Imp"}]}
    )
    fake = FakeSubprocess([(0, payload)])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_list_projects(owner="KKallas")
    assert res["count"] == 1
    assert res["projects"][0]["number"] == 7
    print("test_do_list_projects_parses_projects_object: OK")


async def test_do_list_projects_empty() -> None:
    _reset_config()
    fake = FakeSubprocess([(0, "{}")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_list_projects(owner="nobody")
    assert res["count"] == 0
    assert res["projects"] == []
    print("test_do_list_projects_empty: OK")


async def test_do_create_imp_project_success_parses_result_json() -> None:
    """rc=0 → `created: True` plus the parsed JSON result from the script."""
    _reset_config()
    script_output = json.dumps(
        {
            "project_number": 7,
            "project_owner": "KKallas",
            "project_status": "created",
            "created_fields": ["duration_days", "start_date"],
            "skipped_fields": [],
            "deleted_fields": [],
            "conflicts_ignored": [],
            "on_conflict": "stop",
        }
    )
    fake = FakeSubprocess([(0, script_output)])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_create_imp_project(owner="KKallas")
    assert res["created"] is True
    assert res["exit_code"] == 0
    assert res["result"]["project_number"] == 7
    assert res["result"]["project_status"] == "created"
    # Verify --on-conflict stop was passed (the default)
    assert "--on-conflict" in fake.calls[0]
    idx = fake.calls[0].index("--on-conflict")
    assert fake.calls[0][idx + 1] == "stop"
    print("test_do_create_imp_project_success_parses_result_json: OK")


async def test_do_create_imp_project_surfaces_conflicts_on_rc2() -> None:
    """rc=2 → parse the conflict report and hand it to the LLM."""
    _reset_config()
    conflict_report = json.dumps(
        {
            "status": "conflicts_detected",
            "project_number": 5,
            "project_owner": "KKallas",
            "project_status": "existing",
            "conflicts": [
                {
                    "name": "duration_days",
                    "reason": "wrong_type",
                    "expected_type": "NUMBER",
                    "actual_type": "TEXT",
                    "field_id": "PVTF_abc",
                }
            ],
            "next_steps": "Re-run with --on-conflict delete or fix manually.",
        }
    )
    fake = FakeSubprocess([(2, conflict_report)])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_create_imp_project(owner="KKallas")
    assert res["created"] is False
    assert res["exit_code"] == 2
    assert res["project_number"] == 5
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["name"] == "duration_days"
    # The LLM needs steering on what to do — the tool includes a clear
    # instruction about asking the admin.
    assert "admin" in res["instruction_for_agent"].lower()
    assert "delete" in res["instruction_for_agent"].lower()
    print("test_do_create_imp_project_surfaces_conflicts_on_rc2: OK")


async def test_do_create_imp_project_delete_mode_forwards_flag() -> None:
    """When admin chooses overwrite, the tool passes on_conflict=delete."""
    _reset_config()
    script_output = json.dumps(
        {
            "project_number": 5,
            "project_owner": "KKallas",
            "project_status": "existing",
            "created_fields": ["duration_days"],
            "skipped_fields": [],
            "deleted_fields": ["duration_days"],
            "conflicts_ignored": [],
            "on_conflict": "delete",
        }
    )
    fake = FakeSubprocess([(0, script_output)])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_create_imp_project(
        owner="KKallas", on_conflict="delete"
    )
    assert res["created"] is True
    assert res["result"]["deleted_fields"] == ["duration_days"]
    # Verify --on-conflict delete was passed through to the subprocess
    idx = fake.calls[0].index("--on-conflict")
    assert fake.calls[0][idx + 1] == "delete"
    print("test_do_create_imp_project_delete_mode_forwards_flag: OK")


async def test_do_create_imp_project_surfaces_gh_error_on_rc1() -> None:
    """rc=1 → gh error. Surface the message so the LLM can help the admin."""
    _reset_config()
    fake = FakeSubprocess([(1, "gh: token lacks 'project' scope")])
    setup_agent._run_subprocess = fake
    res = await setup_agent.do_create_imp_project(owner="KKallas")
    assert res["created"] is False
    assert res["exit_code"] == 1
    assert "scope" in res["error"].lower()
    print("test_do_create_imp_project_surfaces_gh_error_on_rc1: OK")


async def test_do_detect_repo_from_git_returns_dict_shape() -> None:
    _reset_config()
    res = await setup_agent.do_detect_repo_from_git()
    # Just verify the shape — value depends on the checkout's git remote
    assert "repo" in res
    assert "found" in res
    assert res["found"] == (res["repo"] is not None)
    print("test_do_detect_repo_from_git_returns_dict_shape: OK")


# ---------- runner ----------


async def amain() -> None:
    tests = [
        test_do_mark_setup_complete_refuses_without_repo,
        test_do_mark_setup_complete_happy_path,
        test_do_configure_loop_saves_valid_settings,
        test_do_configure_loop_rejects_short_interval,
        test_do_configure_loop_rejects_zero_max_tasks,
        test_do_set_admin_password_rejects_short,
        test_do_set_admin_password_hashes_and_persists,
        test_do_gh_auth_login_returns_instruction,
        test_do_claude_auth_login_returns_instruction,
        test_do_claude_auth_status_reports_env_and_sdk,
        test_do_gh_auth_status_authenticated,
        test_do_gh_auth_status_not_authenticated,
        test_do_list_repos_parses_json,
        test_do_list_repos_handles_gh_error,
        test_do_set_repo_rejects_bad_shape,
        test_do_set_repo_verifies_then_writes,
        test_do_set_repo_refuses_when_gh_rejects,
        test_do_list_projects_parses_projects_object,
        test_do_list_projects_empty,
        test_do_create_imp_project_success_parses_result_json,
        test_do_create_imp_project_surfaces_conflicts_on_rc2,
        test_do_create_imp_project_delete_mode_forwards_flag,
        test_do_create_imp_project_surfaces_gh_error_on_rc1,
        test_do_detect_repo_from_git_returns_dict_shape,
    ]
    for t in tests:
        await t()
    print(f"\nAll {len(tests)} setup-agent tests passed.")


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
