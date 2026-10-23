"""Native GTK4 / Libadwaita interface for the live connection atlas."""
from __future__ import annotations

from collections import Counter
import ipaddress
import math
from pathlib import Path
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from . import __version__
from .monitor import Activity
from .procfs import Connection, Snapshot, read_snapshot


APP_ID = "io.github.stevenmcsorley.NetAtlas"
REFRESH_MS = 1800
HISTORY_SECONDS = 60
DEMO_MODE = False  # enabled only by the isolated screenshot generator


def endpoint_scope(connection: Connection) -> str:
    try:
        address = ipaddress.ip_address(connection.remote.rsplit(":", 1)[0].strip("[]"))
        return "PUBLIC" if address.is_global else "LOCAL / RESERVED"
    except ValueError:
        return "UNKNOWN"


def _label(text: str, css: str = "") -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0)
    if css:
        widget.add_css_class(css)
    return widget


class AtlasWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="NetAtlas")
        self.set_default_size(1180, 820)
        self.set_size_request(820, 700)
        self.activity = Activity()
        self.snapshot = Snapshot(0, (), 0)
        self.selected: Connection | None = None
        self.paused = False
        self.busy = False
        self.search_text = ""
        self.rebuilding_list = False
        self._build_ui()
        self._start_poll()
        GLib.timeout_add(REFRESH_MS, self._tick)

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("app-shell")
        self.set_content(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_top(24)
        header.set_margin_start(30)
        header.set_margin_end(30)
        header.set_margin_bottom(18)
        root.append(header)
        mark = Gtk.Label(label="◉")
        mark.add_css_class("brand-mark")
        header.append(mark)
        titlebox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        header.append(titlebox)
        titlebox.append(_label("NetAtlas", "app-title"))
        titlebox.append(_label("Your computer’s network, made visible", "app-subtitle"))
        self.live_pill = _label("  SCANNING  ", "pill-demo" if DEMO_MODE else "pill-live")
        if DEMO_MODE:
            self.live_pill.set_text("  DEMO DATA · SYNTHETIC  ")
        self.live_pill.set_hexpand(True)
        self.live_pill.set_halign(Gtk.Align.END)
        header.append(self.live_pill)
        self.pause_button = Gtk.ToggleButton(label="Ⅱ  Pause")
        self.pause_button.add_css_class("flat")
        self.pause_button.connect("toggled", self._on_pause)
        header.append(self.pause_button)
        refresh = Gtk.Button(label="↻")
        refresh.set_tooltip_text("Refresh connection snapshot")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self._start_poll())
        header.append(refresh)

        self.scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        root.append(self.scroller)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_start(30)
        content.set_margin_end(30)
        content.set_margin_bottom(28)
        self.scroller.set_child(content)

        self._build_metrics(content)
        self._build_map(content)
        self._build_activity(content)
        self._build_footnote(content)

    def _build_metrics(self, parent):
        row = Gtk.Grid(column_spacing=12, row_spacing=12)
        parent.append(row)
        self.metric_labels = {}
        metrics = [("connections", "LIVE CONNECTIONS", "0"),
                   ("apps", "APPS TALKING", "0"),
                   ("destinations", "DESTINATIONS", "0"),
                   ("new", "NEW · 60 SECONDS", "0")]
        for index, (key, title, initial) in enumerate(metrics):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("metric-card")
            card.set_hexpand(True)
            row.attach(card, index, 0, 1, 1)
            card.append(_label(title, "metric-title"))
            value = _label(initial, "metric-value")
            card.append(value)
            self.metric_labels[key] = value

    def _build_map(self, parent):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panel.add_css_class("panel")
        parent.append(panel)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        panel.append(top)
        top.append(_label("Live connection map", "section-title"))
        top.append(_label("PROCESS  →  DESTINATION", "map-legend"))
        self.map_hint = _label("Waiting for first snapshot…", "muted-small")
        self.map_hint.set_hexpand(True)
        self.map_hint.set_halign(Gtk.Align.END)
        top.append(self.map_hint)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_content_width(900)
        self.canvas.set_content_height(360)
        self.canvas.set_hexpand(True)
        self.canvas.set_draw_func(self._draw_map)
        self.canvas.add_css_class("atlas-canvas")
        panel.append(self.canvas)
        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        panel.append(legend)
        legend.append(_label("●  App process", "legend-process"))
        legend.append(_label("●  Public endpoint", "legend-public"))
        legend.append(_label("●  Local / reserved", "legend-local"))
        legend.append(_label("Lines show observed open sockets, not packet volume", "muted-small"))

    def _build_activity(self, parent):
        grid = Gtk.Grid(column_spacing=16)
        grid.set_column_homogeneous(False)
        parent.append(grid)
        list_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        list_panel.set_hexpand(True)
        list_panel.add_css_class("panel")
        grid.attach(list_panel, 0, 0, 2, 1)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        list_panel.append(title_row)
        title_row.append(_label("Connections", "section-title"))
        self.connection_count = _label("0 observed", "muted-small")
        self.connection_count.set_hexpand(True)
        self.connection_count.set_halign(Gtk.Align.END)
        title_row.append(self.connection_count)
        self.search = Gtk.SearchEntry(placeholder_text="Filter app, address, or protocol…")
        self.search.connect("search-changed", self._on_search)
        list_panel.append(self.search)
        self.flow_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.flow_list.add_css_class("flow-list")
        self.flow_list.connect("row-selected", self._on_row_selected)
        list_panel.append(self.flow_list)
        self.empty = _label("No active internet sockets visible yet. Start a browser or app that connects to the network.", "empty-state")
        self.empty.set_wrap(True)
        list_panel.append(self.empty)

        self.detail_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.detail_panel.add_css_class("panel")
        self.detail_panel.set_size_request(290, -1)
        grid.attach(self.detail_panel, 2, 0, 1, 1)
        self.detail_panel.append(_label("Selected connection", "section-title"))
        self.detail_name = _label("Choose a connection", "detail-title")
        self.detail_name.set_wrap(True)
        self.detail_panel.append(self.detail_name)
        self.detail_scope = _label("", "detail-scope")
        self.detail_panel.append(self.detail_scope)
        self.detail_values = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.detail_panel.append(self.detail_values)
        self.detail_note = _label("NetAtlas reports the socket state visible to your user. It does not inspect payloads or decide whether a connection is malicious.", "muted-small")
        self.detail_note.set_wrap(True)
        self.detail_panel.append(self.detail_note)

    def _build_footnote(self, parent):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        parent.append(row)
        self.visibility_note = _label("Local-only · no packet contents · no reputation lookups", "muted-small")
        self.visibility_note.set_hexpand(True)
        row.append(self.visibility_note)
        row.append(_label(f"NetAtlas {__version__}", "muted-small"))

    def _draw_map(self, area, cr, width, height):
        cr.set_source_rgb(0.045, 0.061, 0.083)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        # Sparse, subdued starfield and orbital guides.
        for i in range(64):
            x = ((i * 149 + 29) % max(width, 1))
            y = ((i * 83 + 17) % max(height, 1))
            cr.set_source_rgba(0.36, 0.54, 0.66, 0.12 if i % 3 else 0.24)
            cr.arc(x, y, 1.0 if i % 5 else 1.7, 0, math.tau)
            cr.fill()
        cx, cy = width / 2, height / 2
        cr.set_line_width(1)
        for radius in (52, 88, 130):
            cr.set_source_rgba(0.29, 0.48, 0.57, 0.15)
            cr.arc(cx, cy, radius, 0, math.tau)
            cr.stroke()

        connections = list(self.activity.connections.values())
        if not connections:
            self._draw_text(cr, "No visible active connections", cx, cy - 4, 15, (0.66, .74, .82), True)
            self._draw_text(cr, "Start an app that uses the network to populate the atlas", cx, cy + 22, 11, (.43, .52, .61))
            self._draw_text(cr, "LOCAL PROCESSES", 28, 32, 10, (.58, .72, .81), True, align="left")
            self._draw_text(cr, "REMOTE ENDPOINTS", width - 28, 32, 10, (.58, .72, .81), True, align="right")
            return

        proc_counts = Counter((c.process.pid, c.process.name) if c.process else (None, "unattributed") for c in connections)
        endpoint_counts = Counter(c.remote for c in connections)
        def endpoint_order(endpoint):
            try:
                ip = ipaddress.ip_address(endpoint.rsplit(":", 1)[0].strip("[]"))
                public = ip.is_global
            except ValueError:
                public = False
            return (not public, -endpoint_counts[endpoint], endpoint)
        endpoints = sorted(endpoint_counts, key=endpoint_order)
        procs = sorted(proc_counts, key=lambda key: (-proc_counts[key], key[1].lower()))[:7]
        ends = endpoints[:10]
        proc_x = width * .30
        end_x = width * .80
        proc_positions = {p: (proc_x, height * (i + 1) / (len(procs) + 1)) for i, p in enumerate(procs)}
        end_positions = {endpoint: (end_x, height * (i + 1) / (len(ends) + 1)) for i, endpoint in enumerate(ends)}

        for connection in connections:
            pkey = ((connection.process.pid, connection.process.name)
                    if connection.process else (None, "unattributed"))
            if pkey not in proc_positions or connection.remote not in end_positions:
                continue
            x1, y1 = proc_positions[pkey]
            x2, y2 = end_positions[connection.remote]
            scope = endpoint_scope(connection)
            color = (0.98, 0.42, 0.21) if scope == "PUBLIC" else (0.24, 0.77, 0.69)
            cr.set_source_rgba(*color, .30)
            cr.set_line_width(1.4)
            cr.move_to(x1 + 8, y1)
            cr.curve_to(cx - 50, y1, cx + 48, y2, x2 - 8, y2)
            cr.stroke()

        self._draw_text(cr, "LOCAL PROCESSES", 28, 32, 10, (.58, .72, .81), True, align="left")
        self._draw_text(cr, "REMOTE ENDPOINTS", width - 28, 32, 10, (.58, .72, .81), True, align="right")
        # The local-device node anchors the flow visually; no geographic guesswork.
        cr.set_source_rgba(.21, .78, .73, .16)
        cr.arc(cx, cy, 35, 0, math.tau)
        cr.fill()
        cr.set_source_rgb(.32, .91, .83)
        cr.arc(cx, cy, 17, 0, math.tau)
        cr.fill()
        self._draw_text(cr, "YOU", cx, cy + 4, 9, (.04, .10, .12), True)
        for key, (x, y) in proc_positions.items():
            cr.set_source_rgba(.32, .91, .83, .17)
            cr.arc(x, y, 15, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(.34, .86, .79)
            cr.arc(x, y, 5, 0, math.tau)
            cr.fill()
            self._draw_text(cr, key[1][:21], x - 22, y + 4, 12, (.93, .96, .99), True, align="right")
        for endpoint in ends:
            try:
                ip = ipaddress.ip_address(endpoint.rsplit(":", 1)[0].strip("[]"))
                public = ip.is_global
            except ValueError:
                public = False
            color = (1.0, .48, .25) if public else (.30, .79, .70)
            x, y = end_positions[endpoint]
            cr.set_source_rgba(*color, .18)
            cr.arc(x, y, 14, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(*color)
            cr.arc(x, y, 5, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(.79, .85, .91)
            self._draw_text(cr, endpoint[:32], x - 24, y + 25, 10, (.72, .79, .86), align="right")
        if len(proc_counts) > len(procs) or len(endpoints) > len(ends):
            self._draw_text(cr, "Map shows busiest 7 apps / first 10 endpoints · full list below", width / 2, height - 15, 9, (.43, .52, .61))

    @staticmethod
    def _draw_text(cr, text, x, y, size, color, bold=False, align="center"):
        cr.select_font_face("Sans", 0, 1 if bold else 0)
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        if align == "center":
            x -= ext[2] / 2
        elif align == "right":
            x -= ext[2]
        cr.set_source_rgb(*color)
        cr.move_to(x, y)
        cr.show_text(text)

    def _start_poll(self):
        if self.busy or self.paused:
            return
        self.busy = True
        self.live_pill.set_text("  MAPPING  ")

        def worker():
            try:
                snapshot = read_snapshot()
                GLib.idle_add(self._receive_snapshot, snapshot, None)
            except Exception as exc:  # Keep the UI alive if procfs is restricted.
                GLib.idle_add(self._receive_snapshot, None, str(exc))
        threading.Thread(target=worker, name="netatlas-procfs", daemon=True).start()

    def _receive_snapshot(self, snapshot, error):
        self.busy = False
        if error:
            self.live_pill.set_text("  MONITOR ERROR  ")
            self.map_hint.set_text(error)
            return GLib.SOURCE_REMOVE
        self.snapshot = snapshot
        cutoff = snapshot.timestamp - HISTORY_SECONDS
        self.activity.update(snapshot)
        recent_events = sum(event.at >= cutoff for event in self.activity.events)
        self.live_pill.set_text("  DEMO DATA · SYNTHETIC  " if DEMO_MODE else "  LIVE · PROCFS  ")
        self.map_hint.set_text(f"Updated just now · {snapshot.hidden_process_count} process owner(s) unavailable")
        connections = list(self.activity.connections.values())
        self.metric_labels["connections"].set_text(str(len(connections)))
        self.metric_labels["apps"].set_text(str(len({c.process.pid for c in connections if c.process})))
        self.metric_labels["destinations"].set_text(str(len({c.remote for c in connections})))
        self.metric_labels["new"].set_text(str(recent_events))
        self.connection_count.set_text(f"{len(connections)} currently visible · {snapshot.hidden_process_count} unattributed")
        self._refresh_list()
        if DEMO_MODE and self.flow_list.get_row_at_index(0):
            self.flow_list.select_row(self.flow_list.get_row_at_index(0))
        self.canvas.queue_draw()
        return GLib.SOURCE_REMOVE

    def _tick(self):
        if not self.paused:
            self._start_poll()
        return GLib.SOURCE_CONTINUE

    def _on_pause(self, button):
        self.paused = button.get_active()
        button.set_label("▶  Resume" if self.paused else "Ⅱ  Pause")
        if self.paused:
            self.live_pill.set_text("  PAUSED  ")
        else:
            self._start_poll()

    def _on_search(self, entry):
        self.search_text = entry.get_text().casefold().strip()
        self._refresh_list()

    def _refresh_list(self):
        selected_key = self.selected.key if self.selected else None
        self.rebuilding_list = True
        while child := self.flow_list.get_first_child():
            self.flow_list.remove(child)
        conns = list(self.activity.connections.values())
        shown = 0
        selected_row = None
        for connection in conns:
            process = connection.process
            proc_name = process.name if process else "Unattributed process"
            terms = f"{proc_name} {process.pid if process else ''} {connection.remote} {connection.protocol} {connection.state}".casefold()
            if self.search_text and self.search_text not in terms:
                continue
            row = Gtk.ListBoxRow()
            row.connection = connection
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(10)
            line.set_margin_bottom(10)
            line.set_margin_start(10)
            line.set_margin_end(10)
            row.set_child(line)
            dot = Gtk.Label(label="●")
            dot.add_css_class("flow-dot-public" if endpoint_scope(connection) == "PUBLIC" else "flow-dot-local")
            line.append(dot)
            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            main.set_hexpand(True)
            line.append(main)
            main.append(_label(f"{proc_name}  →  {connection.remote}", "flow-title"))
            pid = f"PID {process.pid}" if process else "Process details unavailable"
            main.append(_label(f"{connection.protocol} · {connection.state} · {pid}", "muted-small"))
            scope = _label(endpoint_scope(connection), "scope-public" if endpoint_scope(connection) == "PUBLIC" else "scope-local")
            line.append(scope)
            self.flow_list.append(row)
            shown += 1
            if selected_key and connection.key == selected_key:
                selected_row = row
        self.empty.set_visible(shown == 0)
        if selected_row is None and DEMO_MODE and self.flow_list.get_row_at_index(0):
            selected_row = self.flow_list.get_row_at_index(0)
        self.flow_list.select_row(selected_row)
        self.selected = selected_row.connection if selected_row else None
        self.rebuilding_list = False
        self._show_detail(self.selected)

    def _on_row_selected(self, _list, row):
        if self.rebuilding_list:
            return
        self.selected = row.connection if row else None
        self._show_detail(self.selected)

    def _show_detail(self, connection):
        while child := self.detail_values.get_first_child():
            self.detail_values.remove(child)
        if not connection:
            self.detail_name.set_text("Choose a connection")
            self.detail_scope.set_text("")
            return
        process = connection.process
        self.detail_name.set_text(connection.remote)
        self.detail_scope.set_text(endpoint_scope(connection))
        values = [("Application", process.name if process else "Unavailable"),
                  ("Process ID", str(process.pid) if process else "—"),
                  ("Processes sharing socket", str(connection.shared_owners)),
                  ("Executable", process.exe if process and process.exe else "Unavailable"),
                  ("Protocol", connection.protocol),
                  ("State", connection.state),
                  ("Local socket", connection.local),
                  ("User ID", str(process.uid) if process and process.uid is not None else "Unavailable")]
        for name, value in values:
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            self.detail_values.append(block)
            block.append(_label(name.upper(), "detail-label"))
            value_label = _label(value, "detail-value")
            value_label.set_wrap(True)
            value_label.set_selectable(True)
            block.append(value_label)


class NetAtlasApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=0)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self.connect("activate", self._activate)

    def _activate(self, *_):
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).parent / "data" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        window = self.props.active_window
        if window is None:
            window = AtlasWindow(self)
        window.present()


def run() -> int:
    app = NetAtlasApplication()
    return app.run(None)
