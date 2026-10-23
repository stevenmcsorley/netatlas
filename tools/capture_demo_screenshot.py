"""Run the desktop UI with conspicuously labelled synthetic screenshot data."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from netatlas import app
from netatlas.procfs import Connection, Process, Snapshot


def demo_snapshot():
    now = time.time()
    apps = [
        Process(4102, "Firefox", "/usr/lib/firefox/firefox", 1000, "10002"),
        Process(5210, "Code", "/usr/share/code/code", 1000, "10010"),
        Process(6301, "Music Player", "/usr/bin/music-player", 1000, "10020"),
        Process(7004, "Sync Client", "/usr/bin/sync-client", 1000, "10030"),
    ]
    samples = [
        (apps[0], "1.1.1.1:443", "ESTABLISHED"),
        (apps[0], "8.8.8.8:443", "ESTABLISHED"),
        (apps[1], "9.9.9.9:443", "ESTABLISHED"),
        (apps[1], "1.1.1.1:443", "ESTABLISHED"),
        (apps[2], "208.67.222.222:443", "ESTABLISHED"),
        (apps[3], "76.76.2.0:443", "ESTABLISHED"),
        (apps[0], "127.0.0.1:9050", "ESTABLISHED"),
        (apps[1], "127.0.0.1:631", "ESTABLISHED"),
    ]
    connections = tuple(
        Connection("TCP", f"192.168.1.{10 + i}:{41000 + i}", remote,
                   state, str(9000 + i), process)
        for i, (process, remote, state) in enumerate(samples)
    )
    return Snapshot(now, connections, 1)


app.DEMO_MODE = True
app.read_snapshot = demo_snapshot
raise SystemExit(app.run())
