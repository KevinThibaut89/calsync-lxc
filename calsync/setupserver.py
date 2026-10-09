"""A one-time setup web server, served on the container's own LAN address.

Why a server at all: Google's consent needs a browser redirect back to a
registered HTTPS URI, and the only browser around is the one the user already
points at the Proxmox web UI. So the container briefly becomes that redirect
target. The same page also drives the Microsoft device-code sign-in, so both
accounts can be connected from one screen.

It is intentionally tiny and stateless apart from one in-memory device-code
request. Nothing here is a long-running service; the installer starts it, the
user connects both accounts, then it is stopped and the systemd timer takes
over.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import google, msgraph
from .config import Config, TokenStore

STATIC_DIR = Path(__file__).parent / "static"


class SetupState:
    """Shared mutable state for the handler (one setup session)."""

    def __init__(self, cfg: Config, tokens: TokenStore):
        self.cfg = cfg
        self.tokens = tokens
        self.lock = threading.Lock()
        self.device: msgraph.DeviceCode | None = None
        self.oauth_state: str | None = None
        self.last_error: str | None = None


def make_handler(state: SetupState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter logs
            pass

        # -- helpers -----------------------------------------------------
        def _send(self, status: int, body: bytes, ctype="text/html; charset=utf-8", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any):
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _redirect(self, location: str):
            self._send(302, b"", extra={"Location": location})

        def _external_base(self) -> str:
            host = self.headers.get("Host") or "localhost"
            return f"https://{host}"

        def _redirect_uri(self) -> str:
            return self._external_base() + "/oauth/google/callback"

        # -- routing -----------------------------------------------------
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._page()
            elif path == "/status":
                self._status()
            elif path == "/ms/poll":
                self._ms_poll()
            elif path == "/google/start":
                self._google_start()
            elif path == "/oauth/google/callback":
                self._google_callback(urllib.parse.parse_qs(parsed.query))
            elif path.startswith("/static/"):
                self._static(path[len("/static/") :])
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/ms/start":
                self._ms_start()
            elif parsed.path == "/google/save":
                self._google_save()
            else:
                self._send(404, b"not found", "text/plain")

        def _body_form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode() if length else ""
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        # -- pages -------------------------------------------------------
        def _page(self):
            html = (STATIC_DIR / "setup.html").read_text("utf-8")
            html = html.replace("{{REDIRECT_URI}}", self._redirect_uri())
            self._send(200, html.encode())

        def _static(self, name: str):
            safe = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in safe.parents or not safe.exists():
                self._send(404, b"not found", "text/plain")
                return
            ctype = "text/css" if name.endswith(".css") else "application/octet-stream"
            self._send(200, safe.read_bytes(), ctype)

        def _status(self):
            t = state.tokens
            self._json(
                200,
                {
                    "microsoft": t.microsoft_connected,
                    "google": t.google_connected,
                    "google_configured": bool(t.google_client_id and t.google_client_secret),
                    "redirect_uri": self._redirect_uri(),
                    "error": state.last_error,
                },
            )

        # -- microsoft ---------------------------------------------------
        def _ms_start(self):
            try:
                dc = msgraph.begin_device_code(
                    state.cfg.ms_client_id, state.cfg.ms_tenant, cfgmod.GRAPH_SCOPES
                )
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            with state.lock:
                state.device = dc
            self._json(
                200,
                {
                    "user_code": dc.user_code,
                    "verification_uri": dc.verification_uri,
                    "message": dc.message,
                },
            )

        def _ms_poll(self):
            with state.lock:
                dc = state.device
            if not dc:
                self._json(400, {"status": "error", "error": "no sign-in in progress"})
                return
            try:
                data = msgraph.poll_device_code(
                    state.cfg.ms_client_id, state.cfg.ms_tenant, dc.device_code
                )
            except msgraph.AuthPending:
                self._json(200, {"status": "pending"})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(200, {"status": "error", "error": str(exc)})
                return
            state.tokens.ms_refresh_token = data.get("refresh_token")
            state.tokens.save()
            with state.lock:
                state.device = None
            self._json(200, {"status": "done"})

        # -- google ------------------------------------------------------
        def _google_save(self):
            form = self._body_form()
            cid = (form.get("client_id") or "").strip()
            secret = (form.get("client_secret") or "").strip()
            if not cid or not secret:
                self._redirect("/?e=google_missing")
                return
            state.tokens.google_client_id = cid
            state.tokens.google_client_secret = secret
            state.tokens.google_redirect_uri = self._redirect_uri()
            state.tokens.save()
            self._google_start()

        def _google_start(self):
            t = state.tokens
            if not (t.google_client_id and t.google_client_secret):
                self._redirect("/?e=google_missing")
                return
            st = secrets.token_urlsafe(16)
            with state.lock:
                state.oauth_state = st
            t.google_redirect_uri = self._redirect_uri()
            t.save()
            url = google.build_auth_url(
                t.google_client_id, t.google_redirect_uri, cfgmod.GOOGLE_SCOPES, st
            )
            self._redirect(url)

        def _google_callback(self, query: dict[str, list[str]]):
            err = query.get("error", [None])[0]
            if err:
                state.last_error = f"Google declined: {err}"
                self._redirect("/?e=google_denied")
                return
            code = query.get("code", [None])[0]
            st = query.get("state", [None])[0]
            if not code or st != state.oauth_state:
                self._redirect("/?e=google_state")
                return
            t = state.tokens
            try:
                data = google.exchange_code(
                    t.google_client_id, t.google_client_secret, code, t.google_redirect_uri
                )
                refresh = data.get("refresh_token")
                if not refresh:
                    raise RuntimeError(
                        "Google did not return a refresh token; remove this app's access "
                        "at myaccount.google.com and try again."
                    )
                t.google_refresh_token = refresh
                # Create (or find) the destination calendar now.
                client = google.GoogleClient(t.google_client_id, t.google_client_secret, refresh)
                t.google_calendar_id = client.find_or_create_calendar(
                    state.cfg.google_calendar_name
                )
                t.save()
                state.last_error = None
            except Exception as exc:  # noqa: BLE001
                state.last_error = str(exc)
                self._redirect("/?e=google_exchange")
                return
            self._redirect("/?ok=google")

    return Handler


def serve(cfg: Config, tokens: TokenStore, host: str, port: int, certfile: str | None, keyfile: str | None):
    state = SetupState(cfg, tokens)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    if certfile and keyfile:
        import ssl

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if certfile else "http"
    print(f"Setup UI on {scheme}://{host}:{port}/ — open it from your browser.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
