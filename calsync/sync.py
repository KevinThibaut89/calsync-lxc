"""The reconciliation engine: make the Google calendar match Outlook's window.

One pass:

1. Pull every event instance in the window from Graph.
2. List every CalSync-managed event already in the Google calendar.
3. Insert new events, patch changed ones (by content hash), delete events whose
   source disappeared from the window (cancelled, deleted, or moved out).

The source id is the join key, so re-runs never create duplicates, and the
whole thing is idempotent: running it twice in a row changes nothing the second
time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import transform
from .google import GoogleClient
from .msgraph import GraphClient


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.updated + self.deleted

    def summary(self) -> str:
        line = (
            f"created={self.created} updated={self.updated} "
            f"deleted={self.deleted} skipped={self.skipped}"
        )
        if self.errors:
            line += f" errors={len(self.errors)}"
        return line


def window_bounds(past_days: int, future_days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=past_days), now + timedelta(days=future_days)


def run_sync(
    graph: GraphClient,
    google: GoogleClient,
    calendar_id: str,
    *,
    past_days: int,
    future_days: int,
    title_prefix: str = "",
    detail: str = "full",
    skip_declined: bool = False,
    now: datetime | None = None,
) -> SyncResult:
    result = SyncResult()
    start, end = window_bounds(past_days, future_days, now)

    # 1. Source events, keyed by stable source id.
    source: dict[str, dict[str, Any]] = {}
    for ev in graph.iter_calendar_view(start, end):
        if not transform.is_syncable(ev, skip_declined=skip_declined):
            continue
        source[transform.source_id(ev)] = ev

    # 2. Everything we already put in the Google calendar.
    existing: dict[str, dict[str, Any]] = {}
    for gev in google.iter_managed_events(
        calendar_id, transform.MARKER_KEY, transform.MARKER_VALUE
    ):
        sid = transform.stored_source_id(gev)
        if not sid:
            continue
        # If two Google events claim the same source id (shouldn't happen),
        # keep the first and delete the rest below as orphans.
        if sid in existing:
            _safe(result, lambda: google.delete_event(calendar_id, gev["id"]), "delete-dup")
            result.deleted += 1
            continue
        existing[sid] = gev

    # 3a. Insert / update.
    for sid, ev in source.items():
        body = transform.to_google_event(ev, title_prefix=title_prefix, detail=detail)
        current = existing.get(sid)
        if current is None:
            if _safe(result, lambda: google.insert_event(calendar_id, body), f"insert {sid}"):
                result.created += 1
        else:
            new_hash = transform.content_hash(body)
            if transform.stored_hash(current) == new_hash:
                result.skipped += 1
            else:
                if _safe(
                    result,
                    lambda: google.update_event(calendar_id, current["id"], body),
                    f"update {sid}",
                ):
                    result.updated += 1

    # 3b. Delete events whose source left the window.
    for sid, gev in existing.items():
        if sid not in source:
            if _safe(
                result, lambda: google.delete_event(calendar_id, gev["id"]), f"delete {sid}"
            ):
                result.deleted += 1

    return result


def _safe(result: SyncResult, fn, label: str) -> bool:
    """Run one API call, recording the error instead of aborting the pass so a
    single bad event can't block the rest."""
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 - we want to keep going
        result.errors.append(f"{label}: {exc}")
        return False
