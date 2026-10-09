"""In-memory fakes standing in for Graph and Google, at the client-object level.

They implement exactly the methods ``calsync.sync.run_sync`` calls, so the
reconciliation logic can be driven without any network. The Google fake mimics
the parts that matter: server-assigned event ids and the private-extended-
property filter used to list managed events.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any, Iterator

from calsync import transform


def graph_event(
    source_id: str,
    subject: str,
    start: str,
    end: str,
    *,
    all_day: bool = False,
    cancelled: bool = False,
    body: str | None = None,
    body_type: str = "text",
    location: str | None = None,
    show_as: str = "busy",
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": source_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "isAllDay": all_day,
        "isCancelled": cancelled,
        "showAs": show_as,
    }
    if body is not None:
        ev["body"] = {"content": body, "contentType": body_type}
    if location is not None:
        ev["location"] = {"displayName": location}
    return ev


class FakeGraph:
    def __init__(self, events: list[dict[str, Any]]):
        self._events = events
        self.refresh_token_value = "ms-refresh"

    def iter_calendar_view(self, start: datetime, end: datetime, page_size: int = 100) -> Iterator[dict[str, Any]]:
        yield from self._events


class FakeGoogle:
    """Minimal Google Calendar fake with insert/update/delete/list semantics."""

    def __init__(self):
        self._events: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.calls: list[str] = []

    # match the real client surface used by sync
    def iter_managed_events(self, calendar_id: str, marker_key: str, marker_value: str) -> Iterator[dict[str, Any]]:
        for ev in list(self._events.values()):
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            if priv.get(marker_key) == marker_value:
                yield ev

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("insert")
        eid = f"g{next(self._ids)}"
        stored = dict(body)
        stored["id"] = eid
        self._events[eid] = stored
        return stored

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("update")
        stored = dict(body)
        stored["id"] = event_id
        self._events[event_id] = stored
        return stored

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.calls.append("delete")
        self._events.pop(event_id, None)

    # test helpers
    def summaries(self) -> dict[str, str]:
        return {
            transform.stored_source_id(ev): ev.get("summary")
            for ev in self._events.values()
        }

    def count(self) -> int:
        return len(self._events)
