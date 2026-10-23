"""Run the desktop UI with conspicuously labelled synthetic screenshot data."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from netatlas import app
from netatlas.procfs import Connection, Process, Snapshot


def demo_snapshot():
    global demo_calls
    demo_calls += 1
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
    if demo_calls > 1:
        # Demonstrate both session events in screenshots, without ever
        # touching the host's real sockets or process data.
        replacement = Connection(
            "TCP", "192.168.1.22:41022", "203.0.113.18:443",
            "ESTABLISHED", "9022", apps[1]
        )
        connections = connections[1:] + (replacement,)
    return Snapshot(now, connections, 1)


demo_calls = 0
app.DEMO_MODE = True
app.read_snapshot = demo_snapshot
render_event_feed = app.AtlasWindow._render_event_feed


def show_demo_activity(window):
    render_event_feed(window)
    if window.activity.events:
        window.inspector_stack.set_visible_child_name("activity")


app.AtlasWindow._render_event_feed = show_demo_activity
raise SystemExit(app.run())
