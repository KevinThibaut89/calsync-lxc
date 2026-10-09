import unittest

from calsync.sync import run_sync
from tests.fakes import FakeGoogle, FakeGraph, graph_event


def _sync(graph_events, google):
    graph = FakeGraph(graph_events)
    return run_sync(graph, google, "cal-1", past_days=7, future_days=60)


class SyncTests(unittest.TestCase):
    def test_initial_insert(self):
        google = FakeGoogle()
        events = [
            graph_event("a", "Standup", "2026-01-02T09:00:00", "2026-01-02T09:15:00"),
            graph_event("b", "Review", "2026-01-02T10:00:00", "2026-01-02T11:00:00"),
        ]
        r = _sync(events, google)
        self.assertEqual((r.created, r.updated, r.deleted, r.skipped), (2, 0, 0, 0))
        self.assertEqual(google.count(), 2)

    def test_rerun_is_idempotent(self):
        google = FakeGoogle()
        events = [graph_event("a", "Standup", "2026-01-02T09:00:00", "2026-01-02T09:15:00")]
        _sync(events, google)
        google.calls.clear()
        r = _sync(events, google)
        self.assertEqual((r.created, r.updated, r.deleted, r.skipped), (0, 0, 0, 1))
        self.assertEqual(google.calls, [])  # no writes at all

    def test_changed_event_updates(self):
        google = FakeGoogle()
        _sync([graph_event("a", "Standup", "2026-01-02T09:00:00", "2026-01-02T09:15:00")], google)
        r = _sync([graph_event("a", "Standup moved", "2026-01-02T09:30:00", "2026-01-02T09:45:00")], google)
        self.assertEqual((r.created, r.updated, r.deleted), (0, 1, 0))
        self.assertEqual(google.summaries(), {"a": "Standup moved"})
        self.assertEqual(google.count(), 1)

    def test_vanished_event_is_deleted(self):
        google = FakeGoogle()
        _sync(
            [
                graph_event("a", "Keep", "2026-01-02T09:00:00", "2026-01-02T09:15:00"),
                graph_event("b", "Drop", "2026-01-02T10:00:00", "2026-01-02T11:00:00"),
            ],
            google,
        )
        r = _sync([graph_event("a", "Keep", "2026-01-02T09:00:00", "2026-01-02T09:15:00")], google)
        self.assertEqual((r.created, r.updated, r.deleted), (0, 0, 1))
        self.assertEqual(list(google.summaries().keys()), ["a"])

    def test_cancelled_occurrence_removed(self):
        google = FakeGoogle()
        _sync([graph_event("a", "Weekly", "2026-01-02T09:00:00", "2026-01-02T09:30:00")], google)
        r = _sync(
            [graph_event("a", "Weekly", "2026-01-02T09:00:00", "2026-01-02T09:30:00", cancelled=True)],
            google,
        )
        self.assertEqual(r.deleted, 1)
        self.assertEqual(google.count(), 0)

    def test_error_on_one_event_does_not_stop_others(self):
        google = FakeGoogle()
        original = google.insert_event

        def flaky(cal, body):
            if body["extendedProperties"]["private"]["calsyncSource"] == "bad":
                raise RuntimeError("boom")
            return original(cal, body)

        google.insert_event = flaky
        events = [
            graph_event("bad", "Bad", "2026-01-02T09:00:00", "2026-01-02T09:15:00"),
            graph_event("ok", "Good", "2026-01-02T10:00:00", "2026-01-02T11:00:00"),
        ]
        r = _sync(events, google)
        self.assertEqual(r.created, 1)
        self.assertEqual(len(r.errors), 1)
        self.assertEqual(list(google.summaries().keys()), ["ok"])


if __name__ == "__main__":
    unittest.main()
