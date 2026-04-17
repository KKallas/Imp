"""server/chat_history.py — multi-turn memory + disk persistence for chats.

Foreman's `dispatch()` spins up a fresh `ClaudeSDKClient` every turn, so
without help from us the agent has zero memory of prior turns in the
same chat session. This module provides that help in four pieces
(KKallas/Imp#45):

  1. `ChatSession` — in-memory turn history, capped so context can't
     grow without bound. The cap drops the oldest turns first.
  2. Disk persistence — sessions serialize to
     `.imp/chats/<created_at>_<id>.json` so they survive restarts and
     can be resumed.
  3. Title generation — a tiny LLM call picks a 3-6 word title for each
     chat after the first assistant reply. `title_source` ("agent" /
     "user" / "fallback") tells the UI whether the title is "real" and
     protects a manual rename from getting clobbered on re-titling.
  4. `history_preamble()` — flattens the stored turns into a compact
     text block that `dispatch()` prepends to the new user message so
     the SDK client sees the prior conversation as context. No tool
     calls are re-executed; the preamble is for reference only.

No chainlit import — `main.py` wires the session into `cl.user_session`.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
CHATS_DIR = ROOT / ".imp" / "chats"

# History cap — oldest turns drop first when either is exceeded.
# A "turn" here is one user OR one assistant entry (not a pair).
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_CHARS = 80_000  # ~20k tokens, leaves headroom under the 200k cap.

# Fallback title used before the agent-titled call runs.
FALLBACK_TITLE = "New chat"

# Stubs older than this (seconds) with zero turns are pruned on startup.
_STUB_MAX_AGE_SECS = 3600  # 1 hour


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_stem(created_at_iso: str) -> str:
    """Turn an ISO timestamp into a filesystem-safe directory stem.
    `2026-04-15T07:43:00+00:00` → `2026-04-15T07-43-00`."""
    # Drop the timezone suffix and replace colons so POSIX filesystems
    # (and Windows, should Imp ever land there) don't choke.
    head = created_at_iso.split("+")[0].split("Z")[0]
    return head.replace(":", "-")


@dataclass
class Turn:
    """A single user or assistant message in a chat.

    `tool_calls` is a list of `{"name": str, "input": dict}` for
    assistant turns that invoked tools, preserved for audit / replay
    inspection. Never consumed by the LLM — tools aren't re-executed
    on resume, they're just visible in the JSON on disk.
    """

    role: str  # "user" | "assistant"
    content: str
    timestamp: str = field(default_factory=_utcnow_iso)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }
        if self.tool_calls:
            d["tool_calls"] = list(self.tool_calls)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=str(data.get("role") or ""),
            content=str(data.get("content") or ""),
            timestamp=str(data.get("timestamp") or _utcnow_iso()),
            tool_calls=list(data.get("tool_calls") or []),
        )


@dataclass
class ChatSession:
    """A single chat, persisted as one JSON file on disk.

    `title_source` controls re-titling precedence:
      - "fallback" — placeholder, safe to replace with an agent-titled
        call after the first assistant reply.
      - "agent"    — auto-generated; may be refreshed on a topic shift
        if the caller wants.
      - "user"     — admin manually renamed; never auto-overwritten.
    """

    id: str
    title: str = FALLBACK_TITLE
    title_source: str = "fallback"  # fallback | agent | user
    created_at: str = field(default_factory=_utcnow_iso)
    last_active_at: str = field(default_factory=_utcnow_iso)
    repo: Optional[str] = None
    turns: list[Turn] = field(default_factory=list)

    # ---- factories ----

    @classmethod
    def new(cls, *, repo: Optional[str] = None, id: Optional[str] = None) -> "ChatSession":
        return cls(id=id or _new_chat_id(), repo=repo)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatSession":
        raw_title = str(data.get("title") or FALLBACK_TITLE)
        return cls(
            id=str(data["id"]),
            title=_strip_date_prefix(raw_title) or FALLBACK_TITLE,
            title_source=str(data.get("title_source") or "fallback"),
            created_at=str(data.get("created_at") or _utcnow_iso()),
            last_active_at=str(data.get("last_active_at") or _utcnow_iso()),
            repo=data.get("repo"),
            turns=[Turn.from_dict(t) for t in data.get("turns") or []],
        )

    # ---- serialization ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "title_source": self.title_source,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "repo": self.repo,
            "turns": [t.to_dict() for t in self.turns],
        }

    def sidebar_title(self) -> str:
        """Title with a compact date prefix for sidebar display.

        ``[Apr 17 14:32] Issue triage burndown``
        """
        prefix = date_prefix(self.created_at)
        if prefix:
            return f"{prefix} {self.title}"
        return self.title

    def to_thread_dict(self, *, user_id: str = "admin") -> dict[str, Any]:
        """Convert to Chainlit's ThreadDict shape so our JSON-backed
        data layer can serve chat history in the native left sidebar."""
        steps: list[dict[str, Any]] = []
        for i, t in enumerate(self.turns):
            step_type = "user_message" if t.role == "user" else "assistant_message"
            steps.append({
                "id": f"{self.id}_step_{i}",
                "threadId": self.id,
                "name": t.role,
                "type": step_type,
                "output": t.content,
                "createdAt": t.timestamp,
                "input": "",
                "metadata": {},
                "streaming": False,
            })
        return {
            "id": self.id,
            "name": self.sidebar_title(),
            "createdAt": self.created_at,
            "userId": user_id,
            "userIdentifier": "admin",
            "steps": steps,
            "metadata": {
                "title_source": self.title_source,
                "repo": self.repo,
            },
        }

    def filename(self) -> str:
        """`<created_at_safe>_<id>.json` — chronological sort works with
        a plain `ls` because ISO timestamps sort lexically."""
        return f"{_safe_stem(self.created_at)}_{self.id}.json"

    def path(self, base: Optional[Path] = None) -> Path:
        return (base or CHATS_DIR) / self.filename()

    # ---- mutation ----

    def append_turn(
        self,
        role: str,
        content: str,
        *,
        tool_calls: Optional[list[dict[str, Any]]] = None,
    ) -> Turn:
        turn = Turn(role=role, content=content, tool_calls=list(tool_calls or []))
        self.turns.append(turn)
        self.last_active_at = turn.timestamp
        return turn

    def truncate(
        self,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> int:
        """Drop oldest turns until both caps hold. Returns how many got
        dropped so callers can log it if they want."""
        dropped = 0
        while len(self.turns) > max_turns:
            self.turns.pop(0)
            dropped += 1
        while self.turns and _total_chars(self.turns) > max_chars:
            self.turns.pop(0)
            dropped += 1
        return dropped

    def rename(self, title: str, *, by: str = "user") -> None:
        """Set a new title. `by` controls `title_source`:
        "user" locks the title against future agent re-titling;
        "agent" or "fallback" leave it soft."""
        self.title = _strip_date_prefix(title.strip()) or FALLBACK_TITLE
        self.title_source = by

    def needs_agent_title(self) -> bool:
        """Call the titling LLM only when the title is soft (not a
        manual rename) AND there's at least one assistant reply to
        summarize."""
        if self.title_source == "user":
            return False
        return any(t.role == "assistant" and t.content.strip() for t in self.turns)


def _total_chars(turns: Iterable[Turn]) -> int:
    return sum(len(t.content) for t in turns)


import re as _re

_DATE_PREFIX_RE = _re.compile(r"^\[[\w\s:]+\]\s*")


def _strip_date_prefix(title: str) -> str:
    """Remove all ``[Apr 17 14:32]`` prefixes, avoiding double-prefix
    when Chainlit round-trips the sidebar title back through ``rename``."""
    result = title
    while _DATE_PREFIX_RE.match(result):
        result = _DATE_PREFIX_RE.sub("", result, count=1).strip()
    return result


def _new_chat_id() -> str:
    # Short enough to fit in a filename comfortably; unique enough that
    # collisions aren't a concern for one-admin Imp.
    return "chat-" + uuid.uuid4().hex[:12]


# ---------- disk I/O ----------


def ensure_chats_dir(base: Optional[Path] = None) -> Path:
    d = base or CHATS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session: ChatSession, *, base: Optional[Path] = None) -> Path:
    """Write the session as pretty-printed JSON. Overwrites the file
    atomically — write to a sibling tempfile then rename, so a crash
    mid-write can't leave a half-written chat on disk.
    """
    ensure_chats_dir(base)
    final = session.path(base)
    tmp = final.with_suffix(final.suffix + ".tmp")
    tmp.write_text(json.dumps(session.to_dict(), indent=2))
    tmp.replace(final)
    return final


def load_session(chat_id: str, *, base: Optional[Path] = None) -> Optional[ChatSession]:
    """Load a session by `chat_id`. Matches the `_<id>.json` suffix so
    callers don't need to know the created_at prefix."""
    d = base or CHATS_DIR
    if not d.exists():
        return None
    for path in d.glob(f"*_{chat_id}.json"):
        try:
            return ChatSession.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError) as exc:
            print(
                f"[chat_history] corrupt session file {path.name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return None
    return None


def delete_session(chat_id: str, *, base: Optional[Path] = None) -> bool:
    """Delete a session file by id. Returns True if found and deleted."""
    d = base or CHATS_DIR
    if not d.exists():
        return False
    for path in d.glob(f"*_{chat_id}.json"):
        try:
            path.unlink()
            return True
        except OSError as exc:
            print(
                f"[chat_history] delete failed for {path.name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return False
    return False


def list_sessions(
    *, base: Optional[Path] = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Return up to `limit` session summaries, most-recently-active first.

    Each row: `{id, title, title_source, last_active_at, created_at,
    turn_count, path}`. Rows for unparseable files are skipped (with a
    warning on stderr) rather than failing the whole listing.
    """
    d = base or CHATS_DIR
    if not d.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            rows.append(
                {
                    "id": str(data.get("id") or ""),
                    "title": str(data.get("title") or FALLBACK_TITLE),
                    "title_source": str(data.get("title_source") or "fallback"),
                    "last_active_at": str(data.get("last_active_at") or ""),
                    "created_at": str(data.get("created_at") or ""),
                    "turn_count": len(data.get("turns") or []),
                    "path": str(path),
                }
            )
        except (json.JSONDecodeError, KeyError) as exc:
            print(
                f"[chat_history] skipping unreadable {path.name}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    rows.sort(key=lambda r: r["last_active_at"], reverse=True)
    return rows[:limit]


def latest_session(*, base: Optional[Path] = None) -> Optional[ChatSession]:
    """Return the most-recently-active session that has at least one turn.

    Empty stubs (from ``/new`` with no messages sent yet) are skipped so
    a page refresh reopens the last real conversation, not a blank chat.
    """
    for row in list_sessions(base=base, limit=20):
        if row.get("turn_count", 0) > 0:
            return load_session(row["id"], base=base)
    return None


def prune_stubs(*, base: Optional[Path] = None) -> int:
    """Delete all empty stubs except the most recent one.

    Every server restart creates a new empty stub (Chainlit assigns a
    fresh ``thread_id`` that doesn't match anything on disk).  Keeping
    only the newest prevents the sidebar from filling up with blank
    "New chat" entries.

    Returns the number of files deleted.
    """
    d = base or CHATS_DIR
    if not d.exists():
        return 0
    stubs: list[tuple[str, Path]] = []  # (created_at, path)
    for path in list(d.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, KeyError):
            continue
        turns = data.get("turns") or []
        if len(turns) > 0:
            continue
        created = data.get("created_at") or ""
        stubs.append((created, path))
    if len(stubs) <= 1:
        return 0
    # Sort newest-first, keep only the first, delete the rest.
    stubs.sort(key=lambda s: s[0], reverse=True)
    pruned = 0
    for _, path in stubs[1:]:
        try:
            path.unlink()
            pruned += 1
        except OSError:
            pass
    if pruned:
        print(f"[chat_history] pruned {pruned} empty stub(s)", file=sys.stderr)
    return pruned


def date_prefix(created_at_iso: str) -> str:
    """Format a compact date prefix for sidebar display.

    ``2026-04-17T14:32:05+00:00`` → ``[Apr 17 14:32]``
    """
    try:
        dt = datetime.fromisoformat(created_at_iso)
        return dt.strftime("[%b %d %H:%M]")
    except (ValueError, TypeError):
        return ""


# ---------- preamble for dispatch() ----------


def history_preamble(turns: Iterable[Turn]) -> str:
    """Flatten prior turns into a compact text block the LLM can read
    as "context from earlier in this chat". Deliberately plain prose —
    we don't simulate an API conversation because re-querying prior
    turns through the SDK would re-execute their tool calls. The LLM
    is told these are for context only.
    """
    lines: list[str] = []
    for t in turns:
        if not t.content.strip():
            continue
        label = "User" if t.role == "user" else "Assistant"
        lines.append(f"{label}: {t.content.strip()}")
    if not lines:
        return ""
    body = "\n\n".join(lines)
    return (
        "[Prior conversation in this chat — for context only. Do NOT "
        "re-run tool calls from prior turns; only use this as memory "
        "when the admin's new message references something earlier.]\n\n"
        + body
        + "\n\n[Current turn:]\n"
    )


# ---------- agent-titled chats ----------

# The titling call gets a separate system prompt so the LLM stays on
# task — it's NOT acting as Foreman here, it's a labeling helper.
TITLE_SYSTEM_PROMPT = """\
You are a helper that picks short, descriptive titles for chat \
conversations in a project-management tool. You receive a snippet of a \
conversation and reply with ONLY the title: 3-6 words, no quotes, no \
markdown, no trailing punctuation. The title should capture what the \
conversation is about. If you can't tell, output exactly: Chat\
"""

# How much of the conversation we hand to the titler. Small — this
# is a summarization task, not a full replay, and titling should be
# cheap.
TITLE_CONTEXT_CHARS = 2_000

TitleBackend = Callable[[str, str], Awaitable[str]]
"""(system_prompt, user_prompt) -> title text."""


async def _default_title_backend(system_prompt: str, user_prompt: str) -> str:
    """Call Claude via claude-agent-sdk with NO tools and a 1-turn cap.

    Lazy import so test harnesses don't need the SDK installed to
    exercise the rest of this module.
    """
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=[],
        max_turns=1,
    )

    chunks: list[str] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            usage = getattr(message, "usage", None) or {}
            usage_total["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            usage_total["output_tokens"] += int(usage.get("output_tokens", 0) or 0)

    # Charge the titling call to the token budget so it's visible to
    # the admin just like any other LLM turn. Late import avoids a
    # hard dep when the module is used headless.
    try:
        from server import budgets

        budgets.add_tokens(
            usage_total["input_tokens"], usage_total["output_tokens"]
        )
    except Exception as exc:  # noqa: BLE001 — never block titling on budget I/O
        print(
            f"[chat_history] title-token accounting failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

    return "".join(chunks)


def _sanitize_title(raw: str) -> str:
    """Strip quotes, prose preambles, trailing punctuation. Caps at 60
    chars so a chatty LLM can't produce a novel-length title."""
    t = raw.strip().strip('"').strip("'").strip("*").strip("#").strip()
    # If the model returned multiple lines, keep the first non-empty one.
    for line in t.splitlines():
        line = line.strip()
        if line:
            t = line
            break
    # Trim trailing punctuation.
    while t and t[-1] in ".?!:;,":
        t = t[:-1]
    # Hard cap so the UI doesn't wrap into multiple rows.
    if len(t) > 60:
        t = t[:60].rstrip()
    return t or FALLBACK_TITLE


def _format_for_title(session: ChatSession) -> str:
    """Take the last few turns (up to TITLE_CONTEXT_CHARS) as a prompt."""
    # Walk from the end backwards so the most recent exchange is what
    # the titler sees — matches "what is this chat about NOW".
    picked: list[Turn] = []
    total = 0
    for t in reversed(session.turns):
        block = f"{t.role.capitalize()}: {t.content.strip()}\n"
        if total + len(block) > TITLE_CONTEXT_CHARS and picked:
            break
        picked.append(t)
        total += len(block)
    picked.reverse()
    body = "\n".join(f"{t.role.capitalize()}: {t.content.strip()}" for t in picked)
    return (
        "Here's the conversation so far:\n\n"
        f"{body}\n\n"
        "Suggest a 3-6 word chat title that captures what this "
        "conversation is about. Output only the title, no quotes, no "
        "prose."
    )


async def generate_title(
    session: ChatSession,
    *,
    backend: Optional[TitleBackend] = None,
) -> Optional[str]:
    """Pick a title for `session`. No-op if the admin has manually
    renamed it (title_source == "user"). Mutates the session in place
    on success and returns the new title; returns None on failure or
    when there's nothing to title yet.

    `backend` lets tests swap in a fake that doesn't hit the SDK.
    """
    if not session.needs_agent_title():
        return None
    call = backend or _default_title_backend
    try:
        raw = await call(TITLE_SYSTEM_PROMPT, _format_for_title(session))
    except Exception as exc:  # noqa: BLE001 — never block on titling
        print(
            f"[chat_history] title backend failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    new_title = _sanitize_title(raw)
    # Don't overwrite a manual rename that landed while we were waiting.
    if session.title_source == "user":
        return None
    session.rename(new_title, by="agent")
    return new_title
