"""Read live internet sockets and best-effort process owners from Linux procfs."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import socket
import time


PROC = Path("/proc")
TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV",
    "04": "FIN_WAIT_1", "05": "FIN_WAIT_2", "06": "TIME_WAIT",
    "07": "CLOSED", "08": "CLOSE_WAIT", "09": "LAST_ACK",
    "0A": "LISTEN", "0B": "CLOSING", "0C": "NEW_SYN_RECV",
}


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    exe: str
    uid: int | None
    starttime: str


@dataclass(frozen=True)
class Connection:
    protocol: str
    local: str
    remote: str
    state: str
    inode: str
    process: Process | None
    shared_owners: int = 1

    @property
    def key(self) -> tuple[str, str, str, str, int | None, str]:
        proc = self.process
        return (self.protocol, self.local, self.remote,
                proc.name if proc else "?", proc.pid if proc else None,
                proc.starttime if proc else "")


@dataclass(frozen=True)
class Snapshot:
    timestamp: float
    connections: tuple[Connection, ...]
    hidden_process_count: int


def _address(raw: str, family: int) -> tuple[str, int]:
    address_hex, port_hex = raw.split(":", 1)
    packed = bytes.fromhex(address_hex)
    if family == socket.AF_INET:
        packed = packed[::-1]
    else:
        # procfs prints each 32-bit IPv6 word in native byte order.
        packed = b"".join(packed[index:index + 4][::-1]
                          for index in range(0, len(packed), 4))
    return socket.inet_ntop(family, packed), int(port_hex, 16)


def _format_endpoint(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def parse_socket_table(text: str, protocol: str, family: int) -> list[dict[str, str]]:
    """Parse one /proc/net/{tcp,udp}{,6} table."""
    rows = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            local_ip, local_port = _address(fields[1], family)
            remote_ip, remote_port = _address(fields[2], family)
        except (ValueError, OSError):
            continue
        if not remote_port or ipaddress.ip_address(remote_ip).is_unspecified:
            continue
        # TIME_WAIT and other kernel-only rows have inode 0, so no app can
        # own them; excluding them avoids misleading unattributed entries.
        if fields[9] == "0":
            continue
        rows.append({
            "protocol": protocol,
            "local": _format_endpoint(local_ip, local_port),
            "remote": _format_endpoint(remote_ip, remote_port),
            "state": TCP_STATES.get(fields[3].upper(), fields[3].upper())
                     if protocol == "TCP" else "CONNECTED",
            "inode": fields[9],
        })
    return rows


def _read_identity(pid: int, proc: Path) -> tuple[str, str] | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        # comm is parenthesized and may itself contain spaces or ')'.
        close = stat.rfind(")")
        name = stat[stat.find("(") + 1:close]
        rest = stat[close + 2:].split()
        return name or "unknown", rest[19]  # field 22; `rest` starts at field 3.
    except (OSError, ValueError, IndexError):
        return None


def _read_process(pid: int, proc: Path, name: str, starttime: str) -> Process | None:
    root = proc / str(pid)
    try:
        uid = None
        for line in (root / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                break
        try:
            exe = os.readlink(root / "exe")
        except OSError:
            exe = ""
        executable_name = os.path.basename(exe.removesuffix(" (deleted)"))
        if len(name) >= 15 and executable_name:
            name = executable_name
        return Process(pid, name, exe, uid, starttime)
    except (OSError, ValueError, IndexError):
        return None


def _socket_owners(inodes: set[str], proc: Path) -> tuple[dict[str, list[Process]], int]:
    owners: dict[str, list[Process]] = {}
    hidden = 0
    if not inodes:
        return owners, hidden
    try:
        entries = os.scandir(proc)
    except OSError:
        return owners, len(inodes)
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            before = _read_identity(pid, proc)
            if before is None:
                continue
            matched_inodes: set[str] = set()
            try:
                with os.scandir(proc / str(pid) / "fd") as fds:
                    for fd in fds:
                        try:
                            target = os.readlink(fd.path)
                        except OSError:
                            continue
                        if target.startswith("socket:[") and target.endswith("]"):
                            inode = target[8:-1]
                            if inode in inodes:
                                matched_inodes.add(inode)
            except OSError:
                continue
            if matched_inodes:
                after = _read_identity(pid, proc)
                if after is None or after[1] != before[1]:
                    continue
                else:
                    process = _read_process(pid, proc, before[0], before[1])
                    final = _read_identity(pid, proc)
                    if process and final == before:
                        for inode in matched_inodes:
                            owners.setdefault(inode, []).append(process)
    hidden = sum(1 for inode in inodes if inode not in owners)
    return owners, hidden


def read_snapshot(proc: Path = PROC) -> Snapshot:
    rows: list[dict[str, str]] = []
    for filename, proto, family in (
        ("net/tcp", "TCP", socket.AF_INET),
        ("net/tcp6", "TCP", socket.AF_INET6),
        ("net/udp", "UDP", socket.AF_INET),
        ("net/udp6", "UDP", socket.AF_INET6),
    ):
        try:
            rows.extend(parse_socket_table((proc / filename).read_text(), proto, family))
        except OSError:
            continue
    inodes = {row["inode"] for row in rows}
    owners, hidden = _socket_owners(inodes, proc)
    connections = []
    for row in rows:
        processes = owners.get(row["inode"], [])
        if processes:
            # Forked helpers can share one socket inode. Keep the connection
            # itself singular while exposing how many process owners we found.
            processes.sort(key=lambda item: (item.uid != os.getuid(), item.pid))
            connections.append(Connection(**row, process=processes[0],
                                          shared_owners=len(processes)))
        else:
            connections.append(Connection(**row, process=None))
    connections.sort(key=lambda c: (c.process.name.lower() if c.process else "~",
                                    c.remote, c.protocol))
    return Snapshot(time.time(), tuple(connections), hidden)
