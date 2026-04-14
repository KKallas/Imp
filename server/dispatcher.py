"""server/dispatcher.py — user-facing chat agent that routes to tools.

Bridges between inbound chat text and the existing `intercept.py`
pipeline. One `dispatch(user_text, ...)` call handles the full
intent → (clarify | execute | answer) loop so the admin sees real
answers instead of the keyword-match stubs in `main.py.on_message`.

## Design

The dispatcher is **not** a claude-agent-sdk tool-calling agent. It
mirrors `guard.py`'s pattern: call Claude with a system prompt and the
user's current turn, get back a bare JSON verdict, dispatch on the
`type` field. Three outcomes:

  {"type": "execute", "argv": [...], "rationale": "..."}
  {"type": "clarify", "question": "..."}
  {"type": "answer",  "text": "..."}

Why not SDK tool-use? Three reasons: (1) consistency with guard.py —
same backend shape, same parsing, same test harness; (2) every
side-effecting action goes through `intercept.execute_command`, which
already enforces guard + budget — the dispatcher must never bypass it,
and keeping the LLM tool-less makes that structurally impossible; (3)
easier to unit-test with a fake backend that returns canned JSON.

## Explicit mode

Some user inputs are already unambiguous commands. The dispatcher
recognises two shapes and skips the LLM entirely:

  - `run: <argv>` / `run <argv>` — literal command pass-through
  - keyword-argv: `moderate issue 42`, `solve issue 7`, `fix pr 17` —
    mapped to the matching `99-tools/*.py --issue/--pr N` invocation

Both short-circuit directly to `intercept.execute_command`. No LLM
round-trip, no clarification. The agent trusts directness.

## Clarification loop

If the LLM returns `{"type": "clarify", ...}`, the dispatcher calls
the caller-provided `ask(question) -> answer` coroutine (wired to
`cl.AskUserMessage` in main.py) and feeds the answer back into the
next turn. Bounded by `MAX_CLARIFY_TURNS` so a misbehaving LLM can't
spin forever.

## Token accounting

The backend returns `(text, input_tokens, output_tokens)`. Dispatcher
feeds those into `budgets.add_tokens` after every call so the shared
counter stays honest.

## Pluggable backend

Production uses `claude-agent-sdk` (imported lazily inside
`_default_backend`). Tests inject a fake via `set_backend(fn)` that
returns canned JSON + zero-token counts.

## No chainlit import

This module has no chainlit import. The caller passes `say` / `ask`
callables for UI output — same pattern as `intercept.execute_command`
taking an opaque `step`. Unit tests substitute plain async lambdas.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from . import budgets, intercept


# ---------- pluggable backend ----------

BackendResult = tuple[str, int, int]  # (text, input_tokens, output_tokens)
BackendCallable = Callable[[str, str], Awaitable[BackendResult]]

_backend: Optional[BackendCallable] = None


def set_backend(backend: Optional[BackendCallable]) -> None:
    """Install a custom backend. Pass `None` to restore the default."""
    global _backend
    _backend = backend


def get_backend() -> BackendCallable:
    return _backend or _default_backend


async def _default_backend(system_prompt: str, user_prompt: str) -> BackendResult:
    """Call Claude via claude-agent-sdk with NO tools and a short turn cap.

    Tools are disallowed because every side-effecting action in Imp has
    to flow through `intercept.execute_command` for the guard + budget
    enforcement. A dispatcher that called GitHub tools directly would
    bypass checkpoint B.

    Token usage is extracted from the SDK's ResultMessage so
    `budgets.add_tokens` gets the right numbers per call.
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
        disallowed_tools=list(_DISALLOWED_TOOLS),
        max_turns=1,
    )

    chunks: list[str] = []
    in_tokens = 0
    out_tokens = 0
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            usage = getattr(message, "usage", None) or {}
            in_tokens += int(usage.get("input_tokens", 0) or 0)
            out_tokens += int(usage.get("output_tokens", 0) or 0)
    return "".join(chunks), in_tokens, out_tokens


# Same belt-and-suspenders list as guard.py — if the SDK ever defaults
# a tool to allowed, this denies it. The dispatcher is a reasoning agent,
# not a tool-calling one.
_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)


# ---------- system prompt ----------

MAX_CLARIFY_TURNS = 3
MAX_USER_HISTORY_CHARS = 4000


SYSTEM_PROMPT = """\
You are Foreman, the chat agent for Imp — a self-hosted coding agent that \
manages a GitHub repo.

Your only job is to classify the admin's current turn into ONE of three \
actions and emit a single JSON object describing it. You have NO tools.

Three action types:

1. "execute" — the admin's intent is clear and maps to a concrete shell \
command that Imp should run. Emit:
   {"type": "execute",
    "argv": ["gh", "issue", "view", "42"],
    "rationale": "User asked to view issue 42"}
   The shell command goes through `intercept.execute_command`, which \
enforces the Guard Agent and the three budgets (tokens / edits / tasks). \
Only propose argv lists that Imp's classifier understands: `gh <noun> \
<verb> ...`, or `python 99-tools/{moderate_issues,solve_issues,fix_prs}.py \
...`, or one of the demo-safe commands (echo, ls, date, etc.).

2. "clarify" — you need more information from the admin before you can \
execute or answer. Emit ONE focused question:
   {"type": "clarify", "question": "Which issue number do you want to moderate?"}
   Don't stack multiple questions; one at a time.

3. "answer" — the admin asked a pure question that doesn't require running \
anything. Use the CURRENT STATE block below to answer. Emit:
   {"type": "answer", "text": "Your token budget is 200,000 with 5,000 used."}

Rules:
- Output EXACTLY one JSON object, no prose around it, no markdown fences.
- Keep rationales and answers under 500 characters.
- Never propose destructive argv the admin didn't ask for. If in doubt, \
clarify instead of executing.
- Never propose commands outside Imp's classifier (arbitrary bash, rm, \
curl, etc.) — the classifier will refuse them anyway.
- If the admin is being direct ("run gh issue view 42", "moderate issue \
7"), pick "execute" immediately. Don't second-guess clear instructions.
"""


def _build_user_prompt(
    user_text: str,
    history: list[tuple[str, str]],
    state_blurb: str,
) -> str:
    """Assemble the per-turn user prompt.

    `history` is a list of (question_from_assistant, answer_from_admin)
    pairs from prior clarify rounds. The current `user_text` is the
    latest inbound turn (first turn, or an answer to the most recent
    clarify).
    """
    parts: list[str] = []
    parts.append("<<<CURRENT STATE>>>")
    parts.append(state_blurb)
    parts.append("<<<END CURRENT STATE>>>\n")

    if history:
        parts.append("<<<CLARIFICATION SO FAR>>>")
        for question, answer in history:
            parts.append(f"Assistant asked: {question}")
            parts.append(f"Admin replied: {answer}")
        parts.append("<<<END CLARIFICATION SO FAR>>>\n")

    parts.append("<<<CURRENT TURN>>>")
    parts.append(user_text.strip() or "(empty)")
    parts.append("<<<END CURRENT TURN>>>")

    blob = "\n".join(parts)
    if len(blob) > MAX_USER_HISTORY_CHARS:
        blob = blob[:MAX_USER_HISTORY_CHARS] + "\n...[truncated]"
    return blob


def _current_state_blurb() -> str:
    b = budgets.get_budgets()
    return (
        f"Token budget: {b.tokens_used:,} / {b.tokens_limit:,} used "
        f"({b.remaining('tokens'):,} remaining)\n"
        f"Edit budget: {b.edits_used} / {b.edits_limit} used "
        f"({b.remaining('edits')} remaining)\n"
        f"Task budget: {b.tasks_used} / {b.tasks_limit} used "
        f"({b.remaining('tasks')} remaining)\n"
        f"Manual-mode pause: "
        f"{'ON (writes rejected)' if not intercept.accepting_new_actions() else 'off'}"
    )


# ---------- verdict parsing ----------


@dataclass
class Verdict:
    type: str  # "execute" | "clarify" | "answer" | "error"
    argv: Optional[list[str]] = None
    rationale: Optional[str] = None
    question: Optional[str] = None
    text: Optional[str] = None
    error: Optional[str] = None


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_verdict(raw: str) -> Verdict:
    """Extract a `Verdict` from the model's raw text output.

    Same lenient strategy as `guard._parse_verdict`: try direct
    `json.loads`, fall back to the first balanced-ish `{...}` slice,
    fail closed on anything we can't make sense of.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return Verdict(type="error", error="empty response")

    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()

    obj: Any = None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        for match in _JSON_OBJECT_RE.finditer(stripped):
            try:
                obj = json.loads(match.group(0))
                break
            except json.JSONDecodeError:
                continue

    if not isinstance(obj, dict):
        preview = stripped[:200].replace("\n", " ")
        return Verdict(type="error", error=f"unparseable: {preview!r}")

    kind = str(obj.get("type", "")).strip().lower()

    if kind == "execute":
        argv = obj.get("argv")
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
            return Verdict(
                type="error",
                error=f"execute verdict missing / malformed argv: {argv!r}",
            )
        rationale = str(obj.get("rationale", "")).strip() or "(no rationale)"
        return Verdict(type="execute", argv=argv, rationale=rationale)

    if kind == "clarify":
        question = str(obj.get("question", "")).strip()
        if not question:
            return Verdict(type="error", error="clarify verdict missing question")
        return Verdict(type="clarify", question=question)

    if kind == "answer":
        text = str(obj.get("text", "")).strip()
        if not text:
            return Verdict(type="error", error="answer verdict missing text")
        return Verdict(type="answer", text=text)

    return Verdict(type="error", error=f"unknown verdict type {kind!r}")


# ---------- explicit-mode detection ----------

# `moderate issue N`, `solve issue N`, `fix pr N` — map to the matching
# 99-tools/*.py invocation. Keeps the LLM out of unambiguous requests.
_KEYWORD_PATTERNS: tuple[tuple[re.Pattern[str], list[str]], ...] = (
    (
        re.compile(r"^\s*moderate\s+(?:issue\s+)?#?(\d+)\s*$", re.IGNORECASE),
        ["python", "99-tools/moderate_issues.py", "--issue"],
    ),
    (
        re.compile(r"^\s*solve\s+(?:issue\s+)?#?(\d+)\s*$", re.IGNORECASE),
        ["python", "99-tools/solve_issues.py", "--issue"],
    ),
    (
        re.compile(r"^\s*fix\s+(?:pr\s+)?#?(\d+)\s*$", re.IGNORECASE),
        ["python", "99-tools/fix_prs.py", "--pr"],
    ),
)


def _parse_explicit(text: str) -> Optional[list[str]]:
    """Return an argv if `text` is an explicit command, else None.

    Two shapes:
      - `run: <argv>` / `run <argv>` — literal pass-through
      - `moderate|solve issue N`, `fix pr N` — mapped keywords
    """
    stripped = text.strip()
    low = stripped.lower()

    if low.startswith("run:") or low.startswith("run "):
        tail = stripped[4:].strip() if low.startswith("run:") else stripped[3:].strip()
        if not tail:
            return None
        try:
            return shlex.split(tail)
        except ValueError:
            return None

    for pattern, prefix in _KEYWORD_PATTERNS:
        m = pattern.match(stripped)
        if m:
            return prefix + [m.group(1)]

    return None


# ---------- dispatch ----------


SayFn = Callable[[str], Awaitable[None]]
AskFn = Callable[[str], Awaitable[Optional[str]]]


async def dispatch(
    user_text: str,
    *,
    say: SayFn,
    ask: AskFn,
    step: Optional[Any] = None,
) -> None:
    """Route `user_text` to execute / clarify / answer.

    `say(text)` posts a message back to the admin. `ask(question)`
    prompts the admin and returns the answer (or None on timeout).
    `step` is forwarded to `intercept.execute_command` when the
    dispatcher picks "execute" — a live `cl.Step` the caller already
    opened to stream subprocess output into.
    """
    import sys

    print(
        f"[dispatcher] dispatch called: user_text={user_text!r}", file=sys.stderr
    )

    # 1. Explicit-mode shortcut — no LLM
    argv = _parse_explicit(user_text)
    if argv is not None:
        print(f"[dispatcher] explicit-mode argv={argv!r}", file=sys.stderr)
        await intercept.execute_command(
            argv,
            user_intent=user_text,
            rationale="explicit-mode dispatch (no LLM)",
            kind="explicit",
            step=step,
        )
        return

    # 2. LLM loop (bounded)
    backend = get_backend()
    history: list[tuple[str, str]] = []
    current_turn = user_text

    for turn_idx in range(MAX_CLARIFY_TURNS):
        user_prompt = _build_user_prompt(
            current_turn, history, _current_state_blurb()
        )
        print(
            f"[dispatcher] calling backend (turn {turn_idx + 1}/{MAX_CLARIFY_TURNS})",
            file=sys.stderr,
        )
        try:
            raw, in_tok, out_tok = await backend(SYSTEM_PROMPT, user_prompt)
        except Exception as exc:  # noqa: BLE001 — fail closed on backend error
            print(
                f"[dispatcher] backend raised: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            await say(f"Dispatcher backend error: {exc}")
            return

        print(
            f"[dispatcher] backend returned: in={in_tok} out={out_tok} "
            f"text={raw[:500]!r}{' ...[truncated]' if len(raw) > 500 else ''}",
            file=sys.stderr,
        )

        # Feed token usage into the shared budget counter. Only on
        # non-negative counts; a broken backend returning weird numbers
        # can't poison the counter.
        if in_tok > 0 or out_tok > 0:
            budgets.add_tokens(max(0, in_tok), max(0, out_tok))

        verdict = _parse_verdict(raw)
        print(
            f"[dispatcher] parsed verdict.type={verdict.type} "
            f"error={verdict.error!r}",
            file=sys.stderr,
        )

        if verdict.type == "execute":
            print(
                f"[dispatcher] → execute argv={verdict.argv!r}",
                file=sys.stderr,
            )
            await intercept.execute_command(
                verdict.argv or [],
                user_intent=user_text,
                rationale=verdict.rationale or "dispatcher proposal",
                kind="dispatched",
                step=step,
            )
            return

        if verdict.type == "answer":
            print(
                f"[dispatcher] → answer text={(verdict.text or '')[:200]!r}",
                file=sys.stderr,
            )
            await say(verdict.text or "(empty answer)")
            return

        if verdict.type == "clarify":
            question = verdict.question or "Could you clarify?"
            print(f"[dispatcher] → clarify q={question!r}", file=sys.stderr)
            answer = await ask(question)
            if answer is None:
                await say("No response received — abandoning this turn.")
                return
            history.append((question, answer))
            current_turn = answer
            continue

        # error
        print(
            f"[dispatcher] → error: {verdict.error}", file=sys.stderr
        )
        await say(f"Dispatcher couldn't parse the model's reply: {verdict.error}")
        return

    print("[dispatcher] clarify loop exhausted", file=sys.stderr)
    await say(
        "I asked for clarification several times without landing on a clear "
        "action. Try rephrasing the request more concretely."
    )
