import os
import stat
import tempfile
import unittest
from pathlib import Path

from calsync.config import Config, TokenStore


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = Config(state_dir=Path("/tmp/x"))
        self.assertEqual(cfg.window_past_days, 7)
        self.assertEqual(cfg.window_future_days, 60)
        self.assertEqual(cfg.google_calendar_name, "Work (Outlook)")

    def test_load_sectioned_toml(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.toml").write_text(
                "[sync]\npast_days = 14\nfuture_days = 30\ninterval_minutes = 5\n"
                '[google]\ncalendar_name = "Office"\n'
                '[microsoft]\ntenant = "contoso.com"\n'
            )
            cfg = Config.load(Path(d))
            self.assertEqual(cfg.window_past_days, 14)
            self.assertEqual(cfg.window_future_days, 30)
            self.assertEqual(cfg.poll_interval_minutes, 5)
            self.assertEqual(cfg.google_calendar_name, "Office")
            self.assertEqual(cfg.ms_tenant, "contoso.com")


class TokenStoreTests(unittest.TestCase):
    def test_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tokens.json"
            store = TokenStore.load(path)
            self.assertFalse(store.microsoft_connected)
            store.ms_refresh_token = "ms"
            store.google_refresh_token = "g"
            store.google_client_id = "cid"
            store.google_client_secret = "secret"
            store.google_calendar_id = "cal"
            store.save()

            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

            again = TokenStore.load(path)
            self.assertTrue(again.microsoft_connected)
            self.assertTrue(again.google_connected)
            self.assertEqual(again.google_client_id, "cid")
            self.assertEqual(again.google_calendar_id, "cal")

    def test_secret_not_in_plain_listing_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tokens.json"
            store = TokenStore.load(path)
            store.ms_refresh_token = "ms"
            store.save()
            text = path.read_text()
            # Only keys that were set should be written (no null noise).
            self.assertIn("ms_refresh_token", text)
            self.assertNotIn("google_refresh_token", text)


if __name__ == "__main__":
    unittest.main()
