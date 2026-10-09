"""Microsoft Graph client: device-code auth and calendar reads.

Auth emulates a real calendar client: it uses the public "Microsoft Office"
client id with the OAuth 2.0 device-code grant, so no Azure app registration or
admin consent is required (the sign-in appears as "Microsoft Office" in tenant
logs). A tenant can still block this with Conditional Access, in which case the
client id is overridable to the user's own registered app.

Only the read scope ``Calendars.Read`` is requested — CalSync never writes to
Outlook.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from . import http

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


class AuthPending(Exception):
    """Raised while the user has not yet completed the device-code sign-in."""


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    message: str


def begin_device_code(client_id: str, tenant: str, scopes: str) -> DeviceCode:
    """Start the device-code flow. The caller shows ``user_code`` /
    ``verification_uri`` (or ``message``) to the user, then polls
    :func:`poll_device_code`."""
    resp = http.request(
        "POST",
        f"{_authority(tenant)}/devicecode",
        form={"client_id": client_id, "scope": scopes},
    )
    return DeviceCode(
        device_code=resp["device_code"],
        user_code=resp["user_code"],
        verification_uri=resp.get("verification_uri") or resp.get("verification_url"),
        interval=int(resp.get("interval", 5)),
        expires_in=int(resp.get("expires_in", 900)),
        message=resp.get("message", ""),
    )


def poll_device_code(client_id: str, tenant: str, device_code: str) -> dict[str, Any]:
    """Poll once for the token. Raises :class:`AuthPending` until the user
    finishes, or :class:`~calsync.http.HttpError` on a terminal error
    (expired/declined)."""
    try:
        return http.request(
            "POST",
            f"{_authority(tenant)}/token",
            form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
            retries=0,
        )
    except http.HttpError as exc:
        if exc.error_code == "authorization_pending":
            raise AuthPending from None
        if exc.error_code == "slow_down":
            time.sleep(5)
            raise AuthPending from None
        raise


def wait_for_device_code(
    client_id: str,
    tenant: str,
    dc: DeviceCode,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until the device-code sign-in completes, returning the token
    payload (``access_token``, ``refresh_token``, ...)."""
    deadline = now() + dc.expires_in
    while True:
        try:
            return poll_device_code(client_id, tenant, dc.device_code)
        except AuthPending:
            if now() >= deadline:
                raise TimeoutError("device-code sign-in timed out") from None
            sleep(dc.interval)


def refresh_token(client_id: str, tenant: str, refresh_token: str, scopes: str) -> dict[str, Any]:
    return http.request(
        "POST",
        f"{_authority(tenant)}/token",
        form={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": scopes,
        },
        retries=0,
    )


class GraphClient:
    """Authenticated Graph client that refreshes its own access token.

    Construct with a refresh token; the client swaps it for an access token on
    first use and when it expires. Microsoft rotates refresh tokens, so after
    any refresh the caller should persist :attr:`refresh_token_value`.
    """

    def __init__(self, client_id: str, tenant: str, refresh_token: str, scopes: str):
        self._client_id = client_id
        self._tenant = tenant
        self._scopes = scopes
        self.refresh_token_value = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0
        self.base = GRAPH_BASE

    def _ensure_token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token
        data = refresh_token(self._client_id, self._tenant, self.refresh_token_value, self._scopes)
        self._access_token = data["access_token"]
        self._expires_at = time.monotonic() + int(data.get("expires_in", 3600))
        # Microsoft issues a new refresh token on most refreshes.
        if data.get("refresh_token"):
            self.refresh_token_value = data["refresh_token"]
        return self._access_token

    def _get(self, url: str, query: dict[str, str] | None = None) -> Any:
        token = self._ensure_token()
        return http.request(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                # Ask Graph to return all times in UTC so we never parse
                # Windows time-zone names.
                "Prefer": 'outlook.timezone="UTC"',
            },
            query=query,
        )

    def iter_calendar_view(
        self, start: datetime, end: datetime, page_size: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Yield every event instance between ``start`` and ``end``.

        ``calendarView`` expands recurring series into individual occurrences
        server-side, so recurrence rules never cross into Google.
        """
        select = ",".join(
            [
                "id",
                "iCalUId",
                "seriesMasterId",
                "subject",
                "bodyPreview",
                "body",
                "start",
                "end",
                "isAllDay",
                "isCancelled",
                "showAs",
                "sensitivity",
                "location",
                "onlineMeeting",
                "onlineMeetingUrl",
                "organizer",
                "lastModifiedDateTime",
            ]
        )
        url: str | None = f"{self.base}/me/calendarView"
        query: dict[str, str] | None = {
            "startDateTime": _iso(start),
            "endDateTime": _iso(end),
            "$select": select,
            "$top": str(page_size),
            "$orderby": "start/dateTime",
        }
        while url:
            data = self._get(url, query)
            for item in data.get("value", []):
                yield item
            url = data.get("@odata.nextLink")
            query = None  # nextLink already carries the query string.

    def me(self) -> dict[str, Any]:
        return self._get(f"{self.base}/me")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
