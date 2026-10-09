"""Command-line entry point.

    calsync setup-server   # one-time web UI to connect both calendars
    calsync connect-microsoft  # terminal device-code sign-in (installer uses this)
    calsync sync           # run one sync pass (the systemd timer calls this)
    calsync status         # show what is connected and the last run
    calsync version
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import google, msgraph
from .config import Config, TokenStore
from .sync import run_sync


def _load(args) -> tuple[Config, TokenStore]:
    cfg = Config.load(Path(args.state_dir) if args.state_dir else None)
    tokens = TokenStore.load(cfg.token_path)
    return cfg, tokens


def cmd_connect_microsoft(args) -> int:
    cfg, tokens = _load(args)
    dc = msgraph.begin_device_code(cfg.ms_client_id, cfg.ms_tenant, cfg.ms_scopes)
    print()
    print("  Sign in to Microsoft 365:")
    print(f"    1. Open  {dc.verification_uri}")
    print(f"    2. Enter code:  {dc.user_code}")
    print()
    print("  Waiting for you to finish… (Ctrl-C to cancel)")
    try:
        data = msgraph.wait_for_device_code(cfg.ms_client_id, cfg.ms_tenant, dc)
    except (TimeoutError, KeyboardInterrupt) as exc:
        print(f"  Sign-in not completed: {exc}", file=sys.stderr)
        return 1
    tokens.ms_refresh_token = data.get("refresh_token")
    tokens.save()
    print("  ✓ Microsoft connected.")
    return 0


def cmd_setup_server(args) -> int:
    from .setupserver import serve

    cfg, tokens = _load(args)
    serve(cfg, tokens, args.host, args.port, args.cert, args.key)
    return 0


def _graph_client(cfg: Config, tokens: TokenStore) -> msgraph.GraphClient:
    return msgraph.GraphClient(
        cfg.ms_client_id, cfg.ms_tenant, tokens.ms_refresh_token, cfgmod.GRAPH_SCOPES
    )


def _google_client(tokens: TokenStore) -> google.GoogleClient:
    return google.GoogleClient(
        tokens.google_client_id, tokens.google_client_secret, tokens.google_refresh_token
    )


def _run_once(cfg: Config, tokens: TokenStore) -> int:
    if not tokens.microsoft_connected:
        print("Microsoft is not connected — run 'calsync connect-microsoft'.", file=sys.stderr)
        return 2
    if not tokens.google_connected or not tokens.google_calendar_id:
        print("Google is not connected — run 'calsync setup-server'.", file=sys.stderr)
        return 2

    graph = _graph_client(cfg, tokens)
    gclient = _google_client(tokens)
    result = run_sync(
        graph,
        gclient,
        tokens.google_calendar_id,
        past_days=cfg.window_past_days,
        future_days=cfg.window_future_days,
        title_prefix=cfg.title_prefix,
    )
    # Microsoft rotates refresh tokens; persist the latest.
    if graph.refresh_token_value and graph.refresh_token_value != tokens.ms_refresh_token:
        tokens.ms_refresh_token = graph.refresh_token_value
        tokens.save()

    _write_last_run(cfg, result)
    print(f"[{_now()}] sync: {result.summary()}")
    for err in result.errors[:10]:
        print(f"  ! {err}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_sync(args) -> int:
    cfg, tokens = _load(args)
    if not args.loop:
        return _run_once(cfg, tokens)
    interval = max(cfg.poll_interval_minutes, 1) * 60
    print(f"Running every {cfg.poll_interval_minutes} min. Ctrl-C to stop.")
    while True:
        try:
            _run_once(cfg, tokens)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"[{_now()}] sync failed: {exc}", file=sys.stderr)
        time.sleep(interval)


def cmd_status(args) -> int:
    cfg, tokens = _load(args)
    last = _read_last_run(cfg)
    print(f"CalSync {__version__}")
    print(f"  state dir:   {cfg.state_dir}")
    print(f"  window:      -{cfg.window_past_days}d … +{cfg.window_future_days}d")
    print(f"  interval:    {cfg.poll_interval_minutes} min")
    print(f"  Microsoft:   {'connected' if tokens.microsoft_connected else 'NOT connected'}")
    g = "connected" if tokens.google_connected else "NOT connected"
    print(f"  Google:      {g}  (calendar: {tokens.google_calendar_id or '—'})")
    if last:
        print(f"  last run:    {last.get('at')}  {last.get('summary')}")
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _write_last_run(cfg: Config, result) -> None:
    try:
        path = cfg.state_dir / "last-run.json"
        path.write_text(
            json.dumps(
                {
                    "at": _now(),
                    "summary": result.summary(),
                    "created": result.created,
                    "updated": result.updated,
                    "deleted": result.deleted,
                    "skipped": result.skipped,
                    "errors": result.errors[:20],
                },
                indent=2,
            )
        )
    except OSError:
        pass


def _read_last_run(cfg: Config) -> dict | None:
    path = cfg.state_dir / "last-run.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="calsync", description="Sync Outlook/M365 calendar to Google.")
    p.add_argument("--state-dir", help="Override the state directory (default /var/lib/calsync).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("connect-microsoft", help="Sign in to Microsoft 365 (device code).")
    s.set_defaults(func=cmd_connect_microsoft)

    s = sub.add_parser("setup-server", help="Serve the one-time setup web UI.")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8777)
    s.add_argument("--cert", help="TLS certificate file (enables HTTPS).")
    s.add_argument("--key", help="TLS private key file.")
    s.set_defaults(func=cmd_setup_server)

    s = sub.add_parser("sync", help="Run a sync pass.")
    s.add_argument("--loop", action="store_true", help="Keep running on the configured interval.")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("status", help="Show connection state and the last run.")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("version", help="Print the version.")
    s.set_defaults(func=cmd_version)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
