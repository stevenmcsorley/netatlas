import unittest

from netatlas.monitor import Activity
from netatlas.procfs import Connection, Process, Snapshot


class ActivityTests(unittest.TestCase):
    def test_new_flow_events_are_deduplicated_and_bounded(self):
        app = Activity(max_events=2)
        process = Process(12, "browser", "/usr/bin/browser", 1000, "987")
        first = Connection("TCP", "127.0.0.1:4000", "8.8.8.8:443", "ESTABLISHED", "1", process)
        second = Connection("TCP", "127.0.0.1:4001", "1.1.1.1:443", "ESTABLISHED", "2", process)
        self.assertEqual(len(app.update(Snapshot(1.0, (first,), 0))), 1)
        self.assertEqual(len(app.update(Snapshot(2.0, (first,), 0))), 0)
        app.update(Snapshot(3.0, (first, second), 0))
        self.assertEqual(len(app.events), 2)
        self.assertEqual(len(app.connections), 2)


if __name__ == "__main__":
    unittest.main()
