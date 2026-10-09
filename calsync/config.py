"""Configuration and token storage.

Two files live under the state directory (``/var/lib/calsync`` in the
container):

* ``config.toml`` — non-secret settings: the sync window, Google calendar name,
  Microsoft tenant/client, poll interval. Read-only at runtime.
* ``tokens.json`` — OAuth refresh tokens and the Google client secret. Written
  with ``0600`` permissions and never logged.

Keeping secrets out of ``config.toml`` means the config can be shown, diffed and
version-controlled by the user without leaking credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib

    def _loads_toml(text: str) -> dict[str, Any]:
        return tomllib.loads(text)
except ModuleNotFoundError:  # 3.9 / 3.10 fallback: a minimal subset reader.

    def _loads_toml(text: str) -> dict[str, Any]:
        return _mini_toml(text)

# The public "Microsoft Office" client. Pre-consented for Microsoft Graph in
# most tenants and valid for the device-code flow, so no app registration is
# needed. Overridable in config for tenants that block it.
DEFAULT_MS_CLIENT_ID = "d3590ed6-52b3-4102-aeff-aad2292ab01c"
DEFAULT_MS_TENANT = "organizations"
GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Calendars.Read"
# Exchange Web Services resource. In tenants that block delegated Graph for
# unmanaged apps (admin-consent required), the Office client is often still
# preauthorized for Exchange — the same path desktop mail clients use.
EWS_SCOPES = "offline_access https://outlook.office365.com/EWS.AccessAsUser.All"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar"

DEFAULT_STATE_DIR = Path(os.environ.get("CALSYNC_STATE_DIR", "/var/lib/calsync"))


@dataclass
class Config:
    state_dir: Path = DEFAULT_STATE_DIR
    # Sync window, relative to "now", in days.
    window_past_days: int = 7
    window_future_days: int = 60
    poll_interval_minutes: int = 10
    # The Google calendar copied events live in. Created if missing.
    google_calendar_name: str = "Work (Outlook)"
    # Microsoft identity.
    ms_tenant: str = DEFAULT_MS_TENANT
    ms_client_id: str = DEFAULT_MS_CLIENT_ID
    # OAuth scopes requested from Microsoft. Default reads the calendar over
    # Graph; set to EWS_SCOPES (config: scopes = "...EWS.AccessAsUser.All") in
    # tenants where Graph needs admin consent but Exchange does not.
    ms_scopes: str = GRAPH_SCOPES
    # Prefix on copied event titles (empty string disables).
    title_prefix: str = ""

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.toml"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "tokens.json"

    @classmethod
    def load(cls, state_dir: Path | None = None) -> "Config":
        sd = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        cfg = cls(state_dir=sd)
        path = sd / "config.toml"
        if path.exists():
            data = _loads_toml(path.read_text("utf-8"))
            for key, value in _flatten(data).items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        cfg.state_dir = sd
        return cfg


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """Accept both flat keys and a ``[sync]``/``[microsoft]``/``[google]`` layout."""
    out: dict[str, Any] = {}
    alias = {
        "past_days": "window_past_days",
        "future_days": "window_future_days",
        "interval_minutes": "poll_interval_minutes",
        "calendar_name": "google_calendar_name",
        "tenant": "ms_tenant",
        "client_id": "ms_client_id",
        "scopes": "ms_scopes",
    }
    for key, value in data.items():
        if isinstance(value, dict):
            for k2, v2 in value.items():
                out[alias.get(k2, k2)] = v2
        else:
            out[alias.get(key, key)] = value
    return out


@dataclass
class TokenStore:
    """Secret material: OAuth tokens and the Google client credentials."""

    path: Path
    ms_refresh_token: str | None = None
    google_refresh_token: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    google_calendar_id: str | None = None
    _extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "TokenStore":
        store = cls(path=path)
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            for key in (
                "ms_refresh_token",
                "google_refresh_token",
                "google_client_id",
                "google_client_secret",
                "google_redirect_uri",
                "google_calendar_id",
            ):
                if key in data:
                    setattr(store, key, data[key])
            store._extra = {
                k: v for k, v in data.items() if not hasattr(store, k) and k != "path"
            }
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "ms_refresh_token": self.ms_refresh_token,
            "google_refresh_token": self.google_refresh_token,
            "google_client_id": self.google_client_id,
            "google_client_secret": self.google_client_secret,
            "google_redirect_uri": self.google_redirect_uri,
            "google_calendar_id": self.google_calendar_id,
            **self._extra,
        }
        data = {k: v for k, v in data.items() if v is not None}
        # Write 0600 atomically so a half-written file can't leak or corrupt.
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def microsoft_connected(self) -> bool:
        return bool(self.ms_refresh_token)

    @property
    def google_connected(self) -> bool:
        return bool(self.google_refresh_token)


def _mini_toml(text: str) -> dict[str, Any]:
    """Parse the small TOML subset CalSync configs use (fallback for <3.11).

    Supports comments, ``[section]`` tables, and ``key = value`` where value is
    a quoted string, integer, or boolean. Not a general TOML parser.
    """
    root: dict[str, Any] = {}
    current = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = root.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = _toml_value(value.split("#", 1)[0].strip())
    return root


def _toml_value(token: str) -> Any:
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
    low = token.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(token)
    except ValueError:
        try:
            return float(token)
        except ValueError:
            return token
