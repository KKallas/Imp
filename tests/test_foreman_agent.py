"""Tests for server/foreman_agent.py — thin agent with security hook.

Run directly: `.venv/bin/python tests/test_foreman_agent.py`

Tests the security hook (can_use_tool callback) which routes Bash
commands through intercept (whitelist) and guard (LLM approval).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import foreman_agent  # noqa: E402


# ---------- security hook tests ----------


async def test_security_allows_non_bash_tools() -> None:
    """Read, Write, Grep, etc. are always allowed."""
    from claude_agent_sdk.types import PermissionResultAllow

    result = await foreman_agent._security_hook("Read", {"file_path": "/tmp/x"}, None)
    assert result.behavior == "allow"

    result = await foreman_agent._security_hook("Write", {"file_path": "/tmp/x"}, None)
    assert result.behavior == "allow"

    result = await foreman_agent._security_hook("Grep", {"pattern": "foo"}, None)
    assert result.behavior == "allow"
    print("test_security_allows_non_bash_tools: OK")


async def test_security_allows_read_commands() -> None:
    """echo, ls etc. are classified as reads and allowed."""
    result = await foreman_agent._security_hook(
        "Bash", {"command": "echo hello"}, None
    )
    assert result.behavior == "allow"

    result = await foreman_agent._security_hook(
        "Bash", {"command": "ls"}, None
    )
    assert result.behavior == "allow"
    print("test_security_allows_read_commands: OK")


async def test_security_redirects_gh_to_tools() -> None:
    """Raw gh commands that have tool scripts are denied with a suggestion."""
    result = await foreman_agent._security_hook(
        "Bash", {"command": "gh issue list --state open"}, None
    )
    assert result.behavior == "deny"
    assert "tool script" in result.message
    assert "list_issues" in result.message

    # gh issue view has no tool script — should be allowed
    result = await foreman_agent._security_hook(
        "Bash", {"command": "gh issue view 42"}, None
    )
    assert result.behavior == "allow"
    print("test_security_redirects_gh_to_tools: OK")


async def test_security_denies_unknown_commands() -> None:
    """Commands not in the whitelist are denied."""
    result = await foreman_agent._security_hook(
        "Bash", {"command": "rm -rf /"}, None
    )
    assert result.behavior == "deny"
    assert "not in whitelist" in result.message
    print("test_security_denies_unknown_commands: OK")


async def test_security_allows_empty_bash() -> None:
    """Empty Bash commands are allowed (no-op)."""
    result = await foreman_agent._security_hook("Bash", {"command": ""}, None)
    assert result.behavior == "allow"
    print("test_security_allows_empty_bash: OK")


async def test_load_system_prompt() -> None:
    """System prompt loads from file."""
    prompt = foreman_agent._load_system_prompt()
    assert "Foreman" in prompt
    assert len(prompt) > 100
    print("test_load_system_prompt: OK")


# ---------- runner ----------


async def amain() -> None:
    tests = [
        test_security_allows_non_bash_tools,
        test_security_allows_read_commands,
        test_security_redirects_gh_to_tools,
        test_security_denies_unknown_commands,
        test_security_allows_empty_bash,
        test_load_system_prompt,
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
