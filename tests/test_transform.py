import unittest

from calsync import transform
from tests.fakes import graph_event


class TransformTests(unittest.TestCase):
    def test_timed_event_maps_to_utc(self):
        ev = graph_event("1", "Standup", "2026-01-02T15:00:00.0000000", "2026-01-02T15:30:00.0000000")
        g = transform.to_google_event(ev)
        self.assertEqual(g["summary"], "Standup")
        self.assertEqual(g["start"], {"dateTime": "2026-01-02T15:00:00Z", "timeZone": "UTC"})
        self.assertEqual(g["end"], {"dateTime": "2026-01-02T15:30:00Z", "timeZone": "UTC"})
        self.assertEqual(g["transparency"], "opaque")

    def test_all_day_uses_date(self):
        ev = graph_event("2", "Holiday", "2026-01-02T00:00:00", "2026-01-03T00:00:00", all_day=True)
        g = transform.to_google_event(ev)
        self.assertEqual(g["start"], {"date": "2026-01-02"})
        self.assertEqual(g["end"], {"date": "2026-01-03"})

    def test_free_busy_becomes_transparent(self):
        ev = graph_event("3", "Focus", "2026-01-02T09:00:00", "2026-01-02T10:00:00", show_as="free")
        self.assertEqual(transform.to_google_event(ev)["transparency"], "transparent")

    def test_title_prefix(self):
        ev = graph_event("4", "1:1", "2026-01-02T09:00:00", "2026-01-02T10:00:00")
        g = transform.to_google_event(ev, title_prefix="[Work] ")
        self.assertEqual(g["summary"], "[Work] 1:1")

    def test_html_body_is_stripped(self):
        ev = graph_event(
            "5",
            "Review",
            "2026-01-02T09:00:00",
            "2026-01-02T10:00:00",
            body="<p>Hello <b>world</b></p><br>line2",
            body_type="html",
        )
        desc = transform.to_google_event(ev)["description"]
        self.assertIn("Hello world", desc)
        self.assertIn("line2", desc)
        self.assertNotIn("<", desc)

    def test_location_and_markers(self):
        ev = graph_event("6", "Lunch", "2026-01-02T12:00:00", "2026-01-02T13:00:00", location="Cafe")
        g = transform.to_google_event(ev)
        self.assertEqual(g["location"], "Cafe")
        priv = g["extendedProperties"]["private"]
        self.assertEqual(priv[transform.MARKER_KEY], transform.MARKER_VALUE)
        self.assertEqual(priv[transform.SOURCE_KEY], "6")
        self.assertIn(transform.HASH_KEY, priv)

    def test_hash_is_stable_and_sensitive(self):
        ev = graph_event("7", "A", "2026-01-02T12:00:00", "2026-01-02T13:00:00")
        h1 = transform.content_hash(transform.to_google_event(ev))
        h2 = transform.content_hash(transform.to_google_event(ev))
        self.assertEqual(h1, h2)
        ev2 = graph_event("7", "A changed", "2026-01-02T12:00:00", "2026-01-02T13:00:00")
        self.assertNotEqual(h1, transform.content_hash(transform.to_google_event(ev2)))

    def test_cancelled_is_not_syncable(self):
        ev = graph_event("8", "Gone", "2026-01-02T12:00:00", "2026-01-02T13:00:00", cancelled=True)
        self.assertFalse(transform.is_syncable(ev))


if __name__ == "__main__":
    unittest.main()
