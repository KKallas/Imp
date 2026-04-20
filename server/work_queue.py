"""server/queue.py — simple work queue for human-in-the-loop items.

Tools push items to the queue. The UI displays them grouped by tool.
Humans resolve items by clicking action buttons. Ultra simple.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = ROOT / ".imp" / "queue.json"

_items: list[dict[str, Any]] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save() -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(_items, indent=2))


def _load() -> None:
    global _items
    if QUEUE_FILE.exists():
        try:
            _items = json.loads(QUEUE_FILE.read_text())
        except (json.JSONDecodeError, KeyError):
            _items = []


# Load on import
_load()


def add(
    *,
    tool: str,
    title: str,
    detail_html: str = "",
    actions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Add an item to the queue. Returns the created item."""
    item: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "tool": tool,
        "title": title,
        "detail_html": detail_html,
        "actions": actions or [{"label": "Done", "action": "done"}],
        "status": "pending",
        "created_at": _now(),
    }
    _items.append(item)
    _save()
    return item


def list_pending() -> list[dict[str, Any]]:
    """Return all pending items."""
    return [i for i in _items if i.get("status") == "pending"]


def list_all() -> list[dict[str, Any]]:
    """Return all items."""
    return list(_items)


def get(item_id: str) -> dict[str, Any] | None:
    """Get an item by ID."""
    for i in _items:
        if i["id"] == item_id:
            return i
    return None


def resolve(item_id: str, action: str) -> dict[str, Any] | None:
    """Resolve an item with the given action. Returns the updated item."""
    for i in _items:
        if i["id"] == item_id:
            i["status"] = action
            i["resolved_at"] = _now()
            _save()
            return i
    return None


def remove(item_id: str) -> bool:
    """Remove an item. Returns True if found."""
    global _items
    before = len(_items)
    _items = [i for i in _items if i["id"] != item_id]
    if len(_items) < before:
        _save()
        return True
    return False


def clear_resolved() -> int:
    """Remove all non-pending items. Returns count removed."""
    global _items
    before = len(_items)
    _items = [i for i in _items if i.get("status") == "pending"]
    removed = before - len(_items)
    if removed:
        _save()
    return removed
