"""Google Calendar client: OAuth web-server flow and event writes.

Google removed the copy-paste (OOB) flow and its device flow does not cover the
Calendar scope, so consent happens in a browser redirect back to the container
itself (the setup server handles the callback). After that the stored refresh
token is enough to run headless forever — provided the OAuth consent screen is
in *production* status, otherwise Google expires the refresh token after ~7
days.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Iterator

from . import http

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_BASE = "https://www.googleapis.com/calendar/v3"


def build_auth_url(client_id: str, redirect_uri: str, scopes: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        # Force a refresh token even if the user consented before.
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, Any]:
    return http.request(
        "POST",
        TOKEN_URL,
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        retries=0,
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    return http.request(
        "POST",
        TOKEN_URL,
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        retries=0,
    )


class GoogleClient:
    """Authenticated Calendar client that refreshes its own access token.

    Google refresh tokens are long-lived and not rotated on refresh, so there
    is nothing to persist after construction.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        data = refresh_access_token(self._client_id, self._client_secret, self._refresh_token)
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + int(data.get("expires_in", 3600))
        return self._access_token

    def _call(self, method: str, path: str, *, json_body: Any = None, query=None) -> Any:
        token = self._ensure_token()
        return http.request(
            method,
            f"{CAL_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json_body=json_body,
            query=query,
        )

    # -- calendars -------------------------------------------------------
    def list_calendars(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            query = {"maxResults": "250"}
            if page_token:
                query["pageToken"] = page_token
            data = self._call("GET", "/users/me/calendarList", query=query)
            out.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def find_or_create_calendar(self, name: str) -> str:
        for cal in self.list_calendars():
            if cal.get("summary") == name:
                return cal["id"]
        created = self._call("POST", "/calendars", json_body={"summary": name})
        return created["id"]

    # -- events ----------------------------------------------------------
    def iter_managed_events(self, calendar_id: str, marker_key: str, marker_value: str) -> Iterator[dict[str, Any]]:
        """Yield events in ``calendar_id`` that CalSync created (tagged with a
        private extended property), including cancelled ones so we can tell
        what still exists."""
        page_token: str | None = None
        while True:
            query = {
                "privateExtendedProperty": f"{marker_key}={marker_value}",
                "maxResults": "250",
                "showDeleted": "false",
                "singleEvents": "true",
            }
            if page_token:
                query["pageToken"] = page_token
            data = self._call("GET", f"/calendars/{_enc(calendar_id)}/events", query=query)
            for item in data.get("items", []):
                yield item
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"/calendars/{_enc(calendar_id)}/events", json_body=body)

    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            "PUT", f"/calendars/{_enc(calendar_id)}/events/{_enc(event_id)}", json_body=body
        )

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            self._call("DELETE", f"/calendars/{_enc(calendar_id)}/events/{_enc(event_id)}")
        except http.HttpError as exc:
            # Already gone is success for our purposes.
            if exc.status not in (404, 410):
                raise


def _enc(value: str) -> str:
    return urllib.parse.quote(value, safe="")
