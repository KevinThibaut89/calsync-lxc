"""Tiny HTTP+JSON helper over urllib, with retries for transient errors.

Everything in CalSync talks to REST APIs (Microsoft Graph, Google, the OAuth
token endpoints). This module is the one place that knows how to make a request,
parse JSON, and turn an HTTP error into something callers can branch on.
"""

from __future__ import annotations

import gzip
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "CalSync/0.1 (+https://github.com/)"
_DEFAULT_TIMEOUT = 30


class HttpError(Exception):
    """An HTTP response with a status >= 400.

    ``body`` is the parsed JSON body when the server sent one, otherwise the
    raw text. ``error_code`` is the machine-readable code both Microsoft and
    Google put in their error payloads, when present.
    """

    def __init__(self, status: int, body: Any, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} for {url}: {self.error_code or body}")

    @property
    def error_code(self) -> str | None:
        b = self.body
        if isinstance(b, dict):
            # Google: {"error": "invalid_grant"} or {"error": {"code": "...", ...}}
            err = b.get("error")
            if isinstance(err, str):
                return err
            if isinstance(err, dict):
                code = err.get("code")
                if code is not None:
                    return str(code)
            # Graph: {"error": {"code": "..."}} is covered above.
        return None


def _build_context(verify_tls: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    verify_tls: bool = True,
    retries: int = 3,
) -> Any:
    """Make an HTTP request and return the parsed JSON (or None for 204).

    Exactly one of ``json_body`` / ``form`` may be given as the body. ``form``
    is url-encoded (used for OAuth token endpoints); ``json_body`` is sent as
    application/json. Raises :class:`HttpError` on a 4xx/5xx the retries don't
    clear, and re-raises ``urllib.error.URLError`` for connection failures.
    """
    if query:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(query)

    data: bytes | None = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    ctx = _build_context(verify_tls)
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return _read_json(resp)
        except urllib.error.HTTPError as exc:
            body = _read_json(exc)
            status = exc.code
            if status in (429, 500, 502, 503, 504) and attempt <= retries:
                time.sleep(_retry_after(exc, attempt))
                continue
            raise HttpError(status, body, url) from None
        except urllib.error.URLError:
            if attempt <= retries:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise


def _retry_after(exc: urllib.error.HTTPError, attempt: int) -> float:
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return min(2 ** attempt, 30)


def _read_json(resp: Any) -> Any:
    raw = resp.read()
    if not raw:
        return None
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" in ctype or text[:1] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text
