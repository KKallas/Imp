"""server/chat_ws.py — WebSocket handler for the lightweight chat UI.

Receives user messages via WebSocket, calls foreman_agent.dispatch(),
and streams tokens + status updates back. Implements TurnUI so the
structured plan/thinking/streaming flow works over WebSocket.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from server import chat_history
from server.foreman_agent import (
    PlanItem,
    TurnUI,
    _format_tool_sig,
    dispatch as foreman_dispatch,
)


class WebSocketTurnUI(TurnUI):
    """TurnUI that sends structured events over WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def _send(self, msg: dict[str, Any]) -> None:
        try:
            await self._ws.send_json(msg)
        except Exception:
            pass

    async def show_plan(self, items: list[PlanItem]) -> None:
        await self._send({
            "type": "status",
            "text": f"Running {_format_tool_sig(items[0].name, items[0].args)}...",
        })

    async def append_plan(self, items: list[PlanItem]) -> None:
        pass

    async def tool_started(self, index: int, item: PlanItem) -> None:
        await self._send({
            "type": "status",
            "text": f"Running {item.name}()...",
        })
        # Insert inline tool marker in the stream
        await self._send({
            "type": "tool_start",
            "name": item.name,
            "args": item.args,
        })

    async def tool_finished(self, index: int, item: PlanItem) -> None:
        await self._send({
            "type": "tool_done",
            "name": item.name,
            "status": item.status,
            "duration": item.duration_s,
            "output": item.output[:2000],  # cap for display
        })
        icon = "\u2705" if item.status == "ok" else "\u274c"
        await self._send({
            "type": "status",
            "text": f"{icon} {item.name}() \u00b7 {item.duration_s:.1f}s",
        })

    async def stream_token(self, token: str) -> None:
        await self._send({"type": "token", "text": token})

    async def stream_end(self, full_text: str) -> None:
        # Final text sent via "done" message from the handler
        pass

    async def thinking_update(self, text: str) -> None:
        await self._send({"type": "status", "text": "Thinking..."})
        await self._send({"type": "thinking", "text": text})


async def handle_ws_chat(ws: WebSocket) -> None:
    """WebSocket endpoint for chat."""
    await ws.accept()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "stop":
                # TODO: cancel in-flight dispatch
                continue

            if msg.get("type") != "message":
                continue

            text = msg.get("text", "").strip()
            if not text:
                continue

            chat_id = msg.get("chat_id")

            # Load or create session
            session = None
            if chat_id:
                session = chat_history.load_session(chat_id)
            if session is None:
                session = chat_history.ChatSession.new()
                chat_history.save_session(session)
                chat_id = session.id
                await ws.send_json({"type": "chat_id", "id": chat_id})

            # Save user turn
            history_turns = list(session.turns)
            session.append_turn("user", text)
            session.truncate()
            chat_history.save_session(session)

            # Status
            await ws.send_json({"type": "status", "text": "Thinking..."})

            # Dispatch
            turn_ui = WebSocketTurnUI(ws)

            async def say(reply_text: str) -> None:
                await ws.send_json({"type": "token", "text": reply_text})

            async def ask(question: str) -> str | None:
                return None  # not supported in lightweight UI yet

            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def thinking(label: str):
                yield None

            async def chart(artifact: dict) -> None:
                # Send chart artifacts as image URLs
                template = artifact.get("template", "chart")
                await ws.send_json({
                    "type": "image",
                    "url": f"/render/{template}",
                    "alt": f"{template} chart",
                })

            try:
                reply = await foreman_dispatch(
                    text,
                    say=say,
                    ask=ask,
                    thinking=thinking,
                    chart=chart,
                    history=history_turns,
                    turn_ui=turn_ui,
                )

                # Save assistant turn
                if reply:
                    session.append_turn("assistant", reply)
                    session.truncate()
                    chat_history.save_session(session)

                    # Auto-title after first reply
                    if session.needs_agent_title():
                        try:
                            await chat_history.generate_title(session)
                            chat_history.save_session(session)
                        except Exception:
                            pass

                await ws.send_json({
                    "type": "done",
                    "full_text": reply or "",
                    "chat_id": chat_id,
                })

            except Exception as exc:
                print(
                    f"[chat_ws] dispatch error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                await ws.send_json({
                    "type": "error",
                    "text": f"{type(exc).__name__}: {exc}",
                })

    except WebSocketDisconnect:
        pass
