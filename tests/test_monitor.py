import unittest

from netatlas.monitor import Activity
from netatlas.procfs import Connection, Process, Snapshot


class ActivityTests(unittest.TestCase):
    def test_tracks_open_and_close_events_without_false_startup_activity(self):
        app = Activity(max_events=2)
        process = Process(12, "browser", "/usr/bin/browser", 1000, "987")
        first = Connection("TCP", "127.0.0.1:4000", "8.8.8.8:443", "ESTABLISHED", "1", process)
        second = Connection("TCP", "127.0.0.1:4001", "1.1.1.1:443", "ESTABLISHED", "2", process)
        # The first sample establishes a baseline; existing sockets aren't
        # falsely described as newly opened by NetAtlas.
        self.assertEqual(app.update(Snapshot(1.0, (first,), 0)), [])
        self.assertEqual(len(app.update(Snapshot(2.0, (first,), 0))), 0)
        app.update(Snapshot(3.0, (first, second), 0))
        closed = app.update(Snapshot(4.0, (second,), 0))
        self.assertEqual([event.kind for event in closed], ["closed"])
        self.assertEqual(len(app.events), 2)
        self.assertEqual(len(app.connections), 1)
        self.assertEqual([event.kind for event in app.events], ["opened", "closed"])

    def test_owner_visibility_change_is_not_a_socket_open_or_close(self):
        activity = Activity()
        process = Process(12, "browser", "/usr/bin/browser", 1000, "987")
        unattributed = Connection("TCP", "127.0.0.1:4000", "8.8.8.8:443", "ESTABLISHED", "1", None)
        attributed = Connection("TCP", "127.0.0.1:4000", "8.8.8.8:443", "ESTABLISHED", "1", process)
        activity.update(Snapshot(1.0, (unattributed,), 1))
        self.assertEqual(activity.update(Snapshot(2.0, (attributed,), 0)), [])
        self.assertEqual(len(activity.events), 0)


if __name__ == "__main__":
    unittest.main()
