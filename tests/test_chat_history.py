"""Tests for server/chat_history.py (KKallas/Imp#45).

Run directly: `.venv/bin/python tests/test_chat_history.py`
No pytest — asserts → exit 0 on success, exit 1 on failure. Matches
the rest of the Imp test suite.

Every test redirects `CHATS_DIR` to a tempdir so the shared
`.imp/chats/` is never touched. The titling-LLM backend is swapped
with a scripted fake — no real SDK calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import chat_history  # noqa: E402


_TMP_BASE = Path(tempfile.mkdtemp(prefix="imp-chat-history-"))


def _fresh_dir() -> Path:
    """One tempdir per test so cross-test file bleed isn't possible."""
    d = Path(tempfile.mkdtemp(prefix="imp-chat-history-", dir=_TMP_BASE))
    return d


# ---------- round-trip serialization ----------


async def test_session_round_trip_preserves_fields() -> None:
    base = _fresh_dir()
    sess = chat_history.ChatSession.new(repo="KKallas/Imp")
    sess.append_turn("user", "moderate issue 42")
    sess.append_turn(
        "assistant",
        "I moderated #42 and added the llm-ready label.",
        tool_calls=[{"name": "run_moderate_issues", "input": {"issue": 42}}],
    )
    sess.rename("Moderate 42 thread", by="user")

    chat_history.save_session(sess, base=base)
    loaded = chat_history.load_session(sess.id, base=base)

    assert loaded is not None
    assert loaded.id == sess.id
    assert loaded.title == "Moderate 42 thread"
    assert loaded.title_source == "user"
    assert loaded.repo == "KKallas/Imp"
    assert len(loaded.turns) == 2
    assert loaded.turns[0].role == "user"
    assert loaded.turns[0].content == "moderate issue 42"
    assert loaded.turns[1].role == "assistant"
    assert loaded.turns[1].tool_calls == [
        {"name": "run_moderate_issues", "input": {"issue": 42}}
    ]
    print("test_session_round_trip_preserves_fields: OK")


async def test_save_writes_filename_with_created_at_prefix() -> None:
    """File name is `<created_at_safe>_<id>.json` so a directory
    listing sorts chronologically without any extra metadata."""
    base = _fresh_dir()
    sess = chat_history.ChatSession.new(repo="x/y")
    path = chat_history.save_session(sess, base=base)
    assert path.parent == base
    assert path.name.endswith(f"_{sess.id}.json")
    # created_at part — no colons, ISO-ish, sortable
    assert ":" not in path.name.split(f"_{sess.id}")[0]
    print("test_save_writes_filename_with_created_at_prefix: OK")


# ---------- truncation past the cap ----------


async def test_truncate_drops_oldest_turns_by_count() -> None:
    sess = chat_history.ChatSession.new()
    # Push in 10 turns, cap at 4 — the 6 oldest should drop.
    for i in range(10):
        sess.append_turn("user" if i % 2 == 0 else "assistant", f"turn {i}")
    dropped = sess.truncate(max_turns=4, max_chars=10_000)
    assert dropped == 6
    assert len(sess.turns) == 4
    # Oldest that survived is `turn 6`.
    assert sess.turns[0].content == "turn 6"
    assert sess.turns[-1].content == "turn 9"
    print("test_truncate_drops_oldest_turns_by_count: OK")


async def test_truncate_drops_by_char_budget_when_turn_count_ok() -> None:
    """Small turn cap isn't tripped, but a per-turn payload blows the
    char budget — the oldest big turn gets evicted first."""
    sess = chat_history.ChatSession.new()
    sess.append_turn("user", "x" * 10_000)
    sess.append_turn("assistant", "y" * 10_000)
    sess.append_turn("user", "short")
    # max_chars=15000 — only the most recent ~15k of content stays.
    dropped = sess.truncate(max_turns=100, max_chars=15_000)
    assert dropped >= 1
    # Oldest big blob got popped; the remaining chars fit under cap.
    total = sum(len(t.content) for t in sess.turns)
    assert total <= 15_000
    print("test_truncate_drops_by_char_budget_when_turn_count_ok: OK")


# ---------- agent-title generation ----------


class _ScriptedTitleBackend:
    """Stand-in for the LLM title backend — records calls and returns
    a scripted response. Tests assert both the recorded prompt AND the
    applied title."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.response


async def test_generate_title_applies_sanitized_title_and_sets_source() -> None:
    sess = chat_history.ChatSession.new()
    sess.append_turn("user", "plan phase 5 wiring")
    sess.append_turn("assistant", "Sure — Phase 5 wires the 99-tools...")
    backend = _ScriptedTitleBackend('"Plan Phase 5 Wiring."\n')
    title = await chat_history.generate_title(sess, backend=backend)
    assert title == "Plan Phase 5 Wiring"  # quotes + period stripped
    assert sess.title == "Plan Phase 5 Wiring"
    assert sess.title_source == "agent"
    # The backend was called with the conversation in the user prompt.
    assert backend.calls, "title backend never invoked"
    _sys, user_prompt = backend.calls[0]
    assert "plan phase 5 wiring" in user_prompt.lower()
    print("test_generate_title_applies_sanitized_title_and_sets_source: OK")


async def test_generate_title_skipped_for_user_renamed_session() -> None:
    """Admin-chosen titles must not be overwritten — the issue calls
    this out explicitly."""
    sess = chat_history.ChatSession.new()
    sess.append_turn("user", "hi")
    sess.append_turn("assistant", "hello")
    sess.rename("My chosen title", by="user")
    backend = _ScriptedTitleBackend("Should Not Be Used")
    result = await chat_history.generate_title(sess, backend=backend)
    assert result is None
    assert sess.title == "My chosen title"
    assert sess.title_source == "user"
    assert backend.calls == []  # never called — saves a token
    print("test_generate_title_skipped_for_user_renamed_session: OK")


async def test_generate_title_skipped_when_no_assistant_reply() -> None:
    """Don't burn tokens titling a chat that has only a user turn —
    there's nothing to summarize yet."""
    sess = chat_history.ChatSession.new()
    sess.append_turn("user", "hi")
    backend = _ScriptedTitleBackend("Too Early")
    result = await chat_history.generate_title(sess, backend=backend)
    assert result is None
    assert sess.title == chat_history.FALLBACK_TITLE
    assert backend.calls == []
    print("test_generate_title_skipped_when_no_assistant_reply: OK")


async def test_generate_title_backend_failure_is_swallowed() -> None:
    """A transient LLM failure should leave the chat untouched, not
    crash the turn."""
    sess = chat_history.ChatSession.new()
    sess.append_turn("user", "x")
    sess.append_turn("assistant", "y")

    async def boom(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("backend down")

    result = await chat_history.generate_title(sess, backend=boom)
    assert result is None
    assert sess.title == chat_history.FALLBACK_TITLE
    assert sess.title_source == "fallback"
    print("test_generate_title_backend_failure_is_swallowed: OK")


async def test_sanitize_title_strips_markdown_and_caps_length() -> None:
    assert chat_history._sanitize_title('"Plan Phase 5"') == "Plan Phase 5"
    assert chat_history._sanitize_title("**Bold Title**") == "Bold Title"
    assert chat_history._sanitize_title("  Padded  ") == "Padded"
    assert chat_history._sanitize_title("Trailing.") == "Trailing"
    # Multi-line: pick first non-empty line.
    assert chat_history._sanitize_title("\n\nFirst\nSecond") == "First"
    # Length cap.
    long = "a" * 200
    out = chat_history._sanitize_title(long)
    assert len(out) <= 60
    # Empty input falls back to the placeholder.
    assert chat_history._sanitize_title("") == chat_history.FALLBACK_TITLE
    print("test_sanitize_title_strips_markdown_and_caps_length: OK")


# ---------- rename ----------


async def test_rename_user_source_blocks_later_agent_rename() -> None:
    sess = chat_history.ChatSession.new()
    sess.rename("My Title", by="user")
    assert sess.title_source == "user"
    # needs_agent_title() must return False once user-renamed.
    sess.append_turn("user", "x")
    sess.append_turn("assistant", "y")
    assert sess.needs_agent_title() is False
    print("test_rename_user_source_blocks_later_agent_rename: OK")


async def test_rename_empty_input_falls_back_to_placeholder() -> None:
    sess = chat_history.ChatSession.new()
    sess.rename("   ", by="user")
    assert sess.title == chat_history.FALLBACK_TITLE
    # Still locks title_source — admin explicitly acted.
    assert sess.title_source == "user"
    print("test_rename_empty_input_falls_back_to_placeholder: OK")


# ---------- list_sessions ----------


async def test_list_sessions_returns_most_recent_first_with_limit() -> None:
    base = _fresh_dir()
    # Build three sessions with distinct last_active_at values.
    for i, when in enumerate(
        ["2026-04-10T00:00:00+00:00",
         "2026-04-12T00:00:00+00:00",
         "2026-04-11T00:00:00+00:00"]
    ):
        s = chat_history.ChatSession.new()
        s.title = f"chat {i}"
        s.last_active_at = when
        chat_history.save_session(s, base=base)
    rows = chat_history.list_sessions(base=base, limit=2)
    assert len(rows) == 2
    assert rows[0]["last_active_at"] == "2026-04-12T00:00:00+00:00"
    assert rows[1]["last_active_at"] == "2026-04-11T00:00:00+00:00"
    # Row shape contains everything the UI needs to render a picker.
    for r in rows:
        for key in ("id", "title", "last_active_at", "turn_count", "path"):
            assert key in r
    print("test_list_sessions_returns_most_recent_first_with_limit: OK")


async def test_list_sessions_skips_corrupt_files_without_crashing() -> None:
    base = _fresh_dir()
    good = chat_history.ChatSession.new()
    good.title = "good"
    chat_history.save_session(good, base=base)
    # Drop a garbage file in alongside — list_sessions must tolerate it.
    (base / "2026-01-01T00-00-00_chat-garbage.json").write_text("not json{")
    rows = chat_history.list_sessions(base=base)
    assert any(r["id"] == good.id for r in rows)
    # The corrupt file is dropped, not represented.
    assert len(rows) == 1
    print("test_list_sessions_skips_corrupt_files_without_crashing: OK")


async def test_load_session_returns_none_for_missing_id() -> None:
    base = _fresh_dir()
    assert chat_history.load_session("chat-missing", base=base) is None
    print("test_load_session_returns_none_for_missing_id: OK")


# ---------- resume restores history into a usable shape ----------


async def test_resume_round_trip_supports_history_preamble() -> None:
    """End-to-end: save a session, load it back, flatten its turns
    into a preamble the dispatch layer can prepend."""
    base = _fresh_dir()
    s = chat_history.ChatSession.new()
    s.append_turn("user", "moderate issue 42")
    s.append_turn("assistant", "Done. Added `llm-ready` label to #42.")
    chat_history.save_session(s, base=base)

    loaded = chat_history.load_session(s.id, base=base)
    assert loaded is not None

    preamble = chat_history.history_preamble(loaded.turns)
    # Preamble includes both roles and the content.
    assert "moderate issue 42" in preamble
    assert "Added `llm-ready` label" in preamble
    # Preamble is prefixed with an explicit "context only" banner so
    # the LLM doesn't re-execute tool calls from prior turns.
    assert "Prior conversation" in preamble
    assert "do not re-run tool calls" in preamble.lower() or \
        "do not re-run" in preamble.lower() or "for context only" in preamble.lower()
    print("test_resume_round_trip_supports_history_preamble: OK")


async def test_history_preamble_is_empty_for_empty_history() -> None:
    """No turns → no preamble → dispatch sends the user text as-is,
    matching the pre-P4.20 behavior exactly."""
    assert chat_history.history_preamble([]) == ""
    print("test_history_preamble_is_empty_for_empty_history: OK")


# ---------- /new rotates: current session persists, new one starts fresh ----------


async def test_rotate_flow_preserves_old_session_on_disk() -> None:
    """Simulates the /new command's disk behavior without touching
    Chainlit: save the old session, create a new one, verify both
    coexist on disk."""
    base = _fresh_dir()
    a = chat_history.ChatSession.new()
    a.append_turn("user", "first chat")
    chat_history.save_session(a, base=base)

    b = chat_history.ChatSession.new()
    b.append_turn("user", "second chat")
    chat_history.save_session(b, base=base)

    rows = chat_history.list_sessions(base=base, limit=20)
    ids = {r["id"] for r in rows}
    assert a.id in ids and b.id in ids
    # Older chat is still readable after rotation.
    restored = chat_history.load_session(a.id, base=base)
    assert restored is not None
    assert restored.turns[0].content == "first chat"
    print("test_rotate_flow_preserves_old_session_on_disk: OK")


# ---------- runner ----------


async def amain() -> None:
    tests = [
        test_session_round_trip_preserves_fields,
        test_save_writes_filename_with_created_at_prefix,
        test_truncate_drops_oldest_turns_by_count,
        test_truncate_drops_by_char_budget_when_turn_count_ok,
        test_generate_title_applies_sanitized_title_and_sets_source,
        test_generate_title_skipped_for_user_renamed_session,
        test_generate_title_skipped_when_no_assistant_reply,
        test_generate_title_backend_failure_is_swallowed,
        test_sanitize_title_strips_markdown_and_caps_length,
        test_rename_user_source_blocks_later_agent_rename,
        test_rename_empty_input_falls_back_to_placeholder,
        test_list_sessions_returns_most_recent_first_with_limit,
        test_list_sessions_skips_corrupt_files_without_crashing,
        test_load_session_returns_none_for_missing_id,
        test_resume_round_trip_supports_history_preamble,
        test_history_preamble_is_empty_for_empty_history,
        test_rotate_flow_preserves_old_session_on_disk,
    ]
    for t in tests:
        await t()
    print(f"\nAll {len(tests)} chat-history tests passed.")


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
