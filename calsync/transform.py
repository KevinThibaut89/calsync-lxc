"""Map a Microsoft Graph event into a Google Calendar event body.

The mapping is deliberately lossless-but-simple: times come from Graph in UTC
(we set the ``Prefer`` header), recurrences are already expanded into single
occurrences, and every copied event carries two private extended properties:

* ``MARKER_KEY`` = ``MARKER_VALUE`` — marks the event as CalSync-managed, so a
  reconciliation pass can list exactly the events it owns.
* ``SOURCE_KEY`` — the Graph instance id, the stable link back to the source.
* ``HASH_KEY`` — a content hash, so unchanged events are skipped on each run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

MARKER_KEY = "calsyncManaged"
MARKER_VALUE = "1"
SOURCE_KEY = "calsyncSource"
HASH_KEY = "calsyncHash"


def is_syncable(graph_event: dict[str, Any]) -> bool:
    """Skip events that should not be copied (cancelled occurrences)."""
    if graph_event.get("isCancelled"):
        return False
    return True


def source_id(graph_event: dict[str, Any]) -> str:
    return graph_event["id"]


def _parse_graph_dt(value: dict[str, Any]) -> datetime:
    """Parse Graph's ``{"dateTime": "...", "timeZone": "UTC"}``.

    We request UTC via the Prefer header, but Graph returns the datetime
    without an offset suffix, so attach UTC explicitly.
    """
    raw = value["dateTime"]
    # Graph returns fractional seconds like 2026-01-02T15:00:00.0000000.
    if "." in raw:
        raw = raw.split(".", 1)[0]
    dt = datetime.fromisoformat(raw)
    tz = (value.get("timeZone") or "UTC").upper()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if tz == "UTC" else timezone.utc)
    return dt.astimezone(timezone.utc)


def _google_time(dt: datetime) -> dict[str, str]:
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"}


def _google_date(dt: datetime) -> dict[str, str]:
    return {"date": dt.strftime("%Y-%m-%d")}


# Graph "showAs" -> Google transparency (free time is "transparent").
_FREE = {"free", "tentative", "workingElsewhere"}


def to_google_event(graph_event: dict[str, Any], *, title_prefix: str = "") -> dict[str, Any]:
    """Build the Google event body for a Graph event (full details)."""
    start_raw = graph_event["start"]
    end_raw = graph_event["end"]
    start_dt = _parse_graph_dt(start_raw)
    end_dt = _parse_graph_dt(end_raw)
    all_day = bool(graph_event.get("isAllDay"))

    summary = graph_event.get("subject") or "(no subject)"
    if title_prefix:
        summary = f"{title_prefix}{summary}"

    description = _description(graph_event)
    location = (graph_event.get("location") or {}).get("displayName") or None

    body: dict[str, Any] = {
        "summary": summary,
        "description": description or None,
        "location": location,
        "start": _google_date(start_dt) if all_day else _google_time(start_dt),
        "end": _google_date(end_dt) if all_day else _google_time(end_dt),
        "transparency": "transparent" if (graph_event.get("showAs") in _FREE) else "opaque",
        "reminders": {"useDefault": False},
        "extendedProperties": {
            "private": {
                MARKER_KEY: MARKER_VALUE,
                SOURCE_KEY: source_id(graph_event),
            }
        },
    }
    body = {k: v for k, v in body.items() if v is not None}
    body["extendedProperties"]["private"][HASH_KEY] = content_hash(body)
    return body


def _description(graph_event: dict[str, Any]) -> str:
    parts: list[str] = []
    body = graph_event.get("body") or {}
    content = (body.get("content") or "").strip()
    ctype = (body.get("contentType") or "").lower()
    if content:
        parts.append(_strip_html(content) if ctype == "html" else content)
    elif graph_event.get("bodyPreview"):
        parts.append(graph_event["bodyPreview"].strip())

    join_url = graph_event.get("onlineMeetingUrl") or (
        (graph_event.get("onlineMeeting") or {}).get("joinUrl")
    )
    if join_url:
        parts.append(f"Join: {join_url}")

    organizer = ((graph_event.get("organizer") or {}).get("emailAddress") or {}).get("address")
    if organizer:
        parts.append(f"Organizer: {organizer}")
    return "\n\n".join(p for p in parts if p)


def _strip_html(html: str) -> str:
    """Very small HTML-to-text pass for Outlook bodies (no external deps)."""
    import re
    from html import unescape

    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = unescape(text)
    # Collapse runs of blank lines / trailing whitespace.
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def content_hash(google_body: dict[str, Any]) -> str:
    """Stable hash of the fields we copy, so unchanged events are skipped.

    Excludes the extendedProperties block itself (which holds the hash).
    """
    relevant = {
        "summary": google_body.get("summary"),
        "description": google_body.get("description"),
        "location": google_body.get("location"),
        "start": google_body.get("start"),
        "end": google_body.get("end"),
        "transparency": google_body.get("transparency"),
    }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def stored_hash(google_event: dict[str, Any]) -> str | None:
    return ((google_event.get("extendedProperties") or {}).get("private") or {}).get(HASH_KEY)


def stored_source_id(google_event: dict[str, Any]) -> str | None:
    return ((google_event.get("extendedProperties") or {}).get("private") or {}).get(SOURCE_KEY)
