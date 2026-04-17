"""server/data_layer.py — Chainlit data layer backed by .imp/chats/ JSON.

Implements `BaseDataLayer` so Chainlit's native left-sidebar shows past
chats and supports click-to-resume, rename, delete — all the UX a
typical chat app has. The JSON files in `.imp/chats/` remain the single
source of truth; no database needed for single-admin Imp.

Register in main.py via::

    @cl.data_layer
    def data_layer():
        from server.data_layer import ImpDataLayer
        return ImpDataLayer()

Then add `@cl.on_chat_resume` to reload the in-memory ChatSession when
the admin clicks a past thread in the sidebar.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.utils import utc_now

from server import chat_history

ADMIN_USER_ID = "admin_id"


class ImpDataLayer(BaseDataLayer):
    """Thin adapter between Chainlit's data-persistence API and our
    JSON-backed `server.chat_history` module. Every method either
    delegates to `chat_history.*` or returns a harmless default — the
    goal is the sidebar + resume flow, not full Literal-style analytics.
    """

    # ---- users (single-admin, trivial) ----

    async def get_user(self, identifier: str) -> Optional[cl.PersistedUser]:
        return cl.PersistedUser(
            id=ADMIN_USER_ID,
            createdAt=utc_now(),
            identifier=identifier,
        )

    async def create_user(self, user: cl.User) -> Optional[cl.PersistedUser]:
        return cl.PersistedUser(
            id=ADMIN_USER_ID,
            createdAt=utc_now(),
            identifier=user.identifier,
        )

    # ---- threads (the core: sidebar listing + resume) ----

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        limit = pagination.first or 20
        rows = chat_history.list_sessions(limit=limit)

        threads: List[ThreadDict] = []
        for row in rows:
            session = chat_history.load_session(row["id"])
            if session is None:
                continue
            threads.append(session.to_thread_dict())

        return PaginatedResponse(
            data=threads,
            pageInfo=PageInfo(
                hasNextPage=len(rows) >= limit,
                startCursor=None,
                endCursor=None,
            ),
        )

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        session = chat_history.load_session(thread_id)
        if session is None:
            return None
        return session.to_thread_dict()

    async def get_thread_author(self, thread_id: str) -> str:
        return "admin"

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        session = chat_history.load_session(thread_id)
        if session is None:
            # Don't create stubs — only _start_new_chat_session() in
            # main.py should create session files.  Chainlit calls
            # update_thread eagerly on every reconnect, which used to
            # litter .imp/chats/ with empty 237-byte stubs.
            return
        if name is not None:
            # Chainlit auto-renames threads to the first user message.
            # Don't let that overwrite an agent- or user-set title.
            if session.title_source == "fallback":
                session.rename(name, by="fallback")
        chat_history.save_session(session)

    async def delete_thread(self, thread_id: str) -> None:
        chat_history.delete_session(thread_id)

    # ---- steps: Chainlit calls these on every message / tool call ----
    # We already persist turns via our own `session.append_turn()` in
    # main.py, so these are no-ops. The data layer receives them anyway
    # because Chainlit's internals call them eagerly.

    async def create_step(self, step_dict: StepDict) -> None:
        pass

    async def update_step(self, step_dict: StepDict) -> None:
        pass

    async def delete_step(self, step_id: str) -> None:
        pass

    # ---- elements (no-op — we don't persist chart images etc.) ----

    async def create_element(self, element: Element) -> None:
        pass

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional[ElementDict]:
        return None

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None) -> None:
        pass

    # ---- feedback (no-op) ----

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return ""

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    # ---- favorites (no-op) ----

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        return []

    async def set_step_favorite(self, step_id: str, user_id: str, favorite: bool) -> None:
        pass

    # ---- misc ----

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    async def delete_user_session(self, id: str) -> bool:
        return True
