"""Bounded connection event history built from real procfs snapshots."""
from collections import deque
from dataclasses import dataclass

from .procfs import Connection, Snapshot


@dataclass(frozen=True)
class Event:
    at: float
    kind: str
    connection: Connection


class Activity:
    def __init__(self, max_events: int = 300):
        self.connections: dict[tuple, Connection] = {}
        self.events: deque[Event] = deque(maxlen=max_events)

    def update(self, snapshot: Snapshot) -> list[Event]:
        current = {connection.key: connection for connection in snapshot.connections}
        added = [Event(snapshot.timestamp, "opened", connection)
                 for key, connection in current.items() if key not in self.connections]
        removed = [Event(snapshot.timestamp, "closed", connection)
                   for key, connection in self.connections.items() if key not in current]
        for event in added:
            self.events.append(event)
        self.connections = current
        return added
