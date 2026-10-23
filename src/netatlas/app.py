"""Native GTK4 / Libadwaita interface for the live connection atlas."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import ipaddress
import math
from pathlib import Path
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from . import __version__
from .monitor import Activity
from .procfs import Connection, Snapshot, read_snapshot


APP_ID = "io.github.stevenmcsorley.NetAtlas"
REFRESH_MS = 5000
HISTORY_SECONDS = 60
MAX_LIST_ROWS = 180
MAX_MAP_EDGES = 160
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
        self.set_default_size(1440, 940)
        self.set_size_request(640, 600)
        self.set_decorated(True)
        self.activity = Activity()
        self.snapshot = Snapshot(0, (), 0)
        self.graph_connections = ()
        self.graph_model = None
        self.list_signature = None
        self.selected: Connection | None = None
        self.paused = False
        self.busy = False
        self.search_text = ""
        self.rebuilding_list = False
        self.animation_phase = 0.0
        self.selected_group_count = 1
        self.list_limit = MAX_LIST_ROWS
        self.sort_mode = "Most sockets"
        self.refresh_ms = REFRESH_MS
        self._build_ui()
        self.maximize()
        self._start_poll()
        self.poll_source = GLib.timeout_add(self.refresh_ms, self._tick)
        GLib.timeout_add(90, self._animate)

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.add_css_class("app-shell")

        header = Adw.HeaderBar()
        header.add_css_class("atlas-header")
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(True)
        mark = Gtk.Label(label="◉")
        mark.add_css_class("brand-mark")
        header.pack_start(mark)
        header.set_title_widget(Adw.WindowTitle(
            title="NetAtlas", subtitle="A live atlas of your network"
        ))
        self.live_pill = _label(" SCANNING ", "pill-demo" if DEMO_MODE else "pill-live")
        if DEMO_MODE:
            self.live_pill.set_text(" DEMO DATA · SYNTHETIC ")
        header.pack_start(self.live_pill)
        self.pause_button = Gtk.ToggleButton(label="Ⅱ  Pause")
        self.pause_button.add_css_class("header-button")
        self.pause_button.connect("toggled", self._on_pause)
        header.pack_end(self.pause_button)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh connection snapshot")
        refresh.add_css_class("header-button")
        refresh.connect("clicked", lambda *_: self._start_poll())
        header.pack_end(refresh)
        self.fullscreen_button = Gtk.ToggleButton()
        self.fullscreen_button.set_child(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic"))
        self.fullscreen_button.set_tooltip_text("Toggle fullscreen (F11)")
        self.fullscreen_button.add_css_class("header-button")
        self.fullscreen_button.connect("toggled", self._on_fullscreen)
        header.pack_end(self.fullscreen_button)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(root)
        self.set_content(toolbar_view)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_margin_top(10)
        body.set_margin_bottom(12)
        body.set_vexpand(True)
        root.append(body)
        self._build_metrics(body)
        self.layout_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, wide_handle=True)
        self.layout_paned.set_vexpand(True)
        self.layout_paned.set_hexpand(True)
        self.layout_paned.set_resize_start_child(True)
        self.layout_paned.set_resize_end_child(False)
        self.layout_paned.add_css_class("atlas-split")
        body.append(self.layout_paned)
        self.layout_paned.set_start_child(self._build_map())
        self.layout_paned.set_end_child(self._build_activity())
        self.layout_paned.connect("notify::height", self._resize_map_split)
        GLib.idle_add(self._set_initial_splits)
        self._build_footnote(body)

    def _build_metrics(self, parent):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("metric-strip")
        row.set_hexpand(True)
        parent.append(row)
        self.metric_labels = {}
        metrics = [("connections", "LIVE CONNECTIONS", "0"),
                   ("apps", "APPS TALKING", "0"),
                   ("destinations", "DESTINATIONS", "0"),
                   ("new", "NEW · 60 SECONDS", "0")]
        for key, title, initial in metrics:
            card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            card.add_css_class("metric-chip")
            card.set_hexpand(True)
            row.append(card)
            card.append(_label(title, "metric-title"))
            value = _label(initial, "metric-value")
            value.set_hexpand(True)
            value.set_halign(Gtk.Align.END)
            card.append(value)
            self.metric_labels[key] = value
        self.metric_labels["new"].set_tooltip_text(
            "New socket connections first observed in the last minute. A connection can begin and end between scans."
        )
        self.metric_labels["connections"].set_tooltip_text(
            "Visible TCP sockets and connected UDP sockets reported by this network namespace. Listeners and Unix sockets are not shown."
        )

    def _build_map(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panel.add_css_class("map-panel")
        panel.set_hexpand(True)
        panel.set_vexpand(True)
        panel.set_size_request(-1, 340)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        panel.append(top)
        top.append(_label("CONNECTION ATLAS", "section-title"))
        map_legend = _label("PROCESS  ↔  ENDPOINT", "map-legend")
        map_legend.set_tooltip_text(
            "This is a live socket map, not a traffic monitor. The animated path is illustrative; packet direction and volume are not measured."
        )
        top.append(map_legend)
        top.append(_label("POLL", "poll-label"))
        self.refresh_dropdown = Gtk.DropDown.new_from_strings(["2 sec", "5 sec", "10 sec"])
        self.refresh_dropdown.set_selected(1)
        self.refresh_dropdown.add_css_class("compact-dropdown")
        self.refresh_dropdown.set_tooltip_text("Choose how often NetAtlas scans process sockets. Longer intervals use less background work.")
        self.refresh_dropdown.connect("notify::selected", self._on_refresh_interval)
        top.append(self.refresh_dropdown)
        self.map_hint = _label("Waiting for first snapshot…", "muted-small")
        self.map_hint.set_hexpand(True)
        self.map_hint.set_halign(Gtk.Align.END)
        self.map_hint.set_tooltip_text(
            "Owner matching is best-effort. Restricted /proc access, process races, and kernel-owned sockets can leave an endpoint unattributed."
        )
        top.append(self.map_hint)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_content_width(900)
        self.canvas.set_content_height(420)
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.set_draw_func(self._draw_map)
        self.canvas.add_css_class("atlas-canvas")
        panel.append(self.canvas)
        legend = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        panel.append(legend)
        legend.append(_label("●  App process", "legend-process"))
        legend.append(_label("●  Public endpoint", "legend-public"))
        legend.append(_label("●  Local / reserved", "legend-local"))
        legend.append(_label("SOCKET ASSOCIATION ONLY  ·  NO PACKET / BANDWIDTH MEASUREMENT", "legend-note"))
        return panel

    def _build_activity(self):
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        split.set_hexpand(True)
        split.set_vexpand(True)
        split.set_resize_start_child(True)
        split.set_resize_end_child(False)
        split.set_size_request(-1, 250)
        split.add_css_class("atlas-split")
        self.activity_paned = split
        list_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        list_panel.set_hexpand(True)
        list_panel.set_vexpand(True)
        list_panel.add_css_class("connection-panel")
        split.set_start_child(list_panel)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        list_panel.append(title_row)
        title_row.append(_label("LIVE CONNECTIONS", "section-title"))
        self.connection_count = _label("0 observed", "muted-small")
        self.connection_count.set_hexpand(True)
        self.connection_count.set_halign(Gtk.Align.END)
        title_row.append(self.connection_count)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        list_panel.append(controls)
        self.search = Gtk.SearchEntry(placeholder_text="Filter app, address, or protocol…")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        controls.append(self.search)
        self.sort_dropdown = Gtk.DropDown.new_from_strings(["Most sockets", "Recently changed", "App name"])
        self.sort_dropdown.add_css_class("compact-dropdown")
        self.sort_dropdown.set_tooltip_text("Sort the visible connection groups")
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        controls.append(self.sort_dropdown)
        self.flow_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.flow_list.add_css_class("flow-list")
        self.flow_list.connect("row-selected", self._on_row_selected)
        self.flow_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow_scroll.set_min_content_height(120)
        self.flow_scroll.set_child(self.flow_list)
        list_panel.append(self.flow_scroll)
        self.load_more = Gtk.Button(label="Load more connections")
        self.load_more.add_css_class("load-more")
        self.load_more.connect("clicked", self._on_load_more)
        self.load_more.set_visible(False)
        list_panel.append(self.load_more)
        self.empty = _label("No active internet sockets visible yet. Start a browser or app that connects to the network.", "empty-state")
        self.empty.set_wrap(True)
        list_panel.append(self.empty)

        inspector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        inspector.add_css_class("connection-panel")
        inspector.set_hexpand(True)
        inspector.set_vexpand(True)
        inspector_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inspector.append(inspector_header)
        inspector_header.append(_label("INSPECTOR", "section-title"))
        self.inspector_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                         transition_duration=160, vexpand=True)
        self.inspector_stack.set_hexpand(True)
        switcher = Gtk.StackSwitcher(stack=self.inspector_stack)
        switcher.set_halign(Gtk.Align.END)
        switcher.set_hexpand(True)
        inspector_header.append(switcher)
        split.set_end_child(inspector)
        detail_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        detail_scroll.set_min_content_width(290)
        detail_scroll.set_min_content_height(180)
        self.detail_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.detail_panel.add_css_class("detail-content")
        detail_scroll.set_child(self.detail_panel)
        self.inspector_stack.add_titled(detail_scroll, "details", "Details")
        self.detail_name = _label("Choose a connection", "detail-title")
        self.detail_name.set_wrap(True)
        self.detail_panel.append(self.detail_name)
        self.detail_scope = _label("", "detail-scope")
        self.detail_panel.append(self.detail_scope)
        self.detail_values = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.detail_panel.append(self.detail_values)
        self.detail_note = _label("The moving route shows app → endpoint association. It does not measure packet direction, contents, or volume.", "muted-small")
        self.detail_note.set_wrap(True)
        self.detail_panel.append(self.detail_note)
        self._build_event_feed()
        inspector.append(self.inspector_stack)
        return split

    def _build_event_feed(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(_label("RECENT CHANGES", "section-title"))
        page.append(_label("New and closed sockets observed during this session. Short-lived connections between scans may be missed.", "muted-small"))
        self.event_empty = _label("No connection changes observed yet.", "empty-state")
        self.event_empty.set_wrap(True)
        page.append(self.event_empty)
        self.event_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.event_list.add_css_class("event-list")
        event_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        event_scroll.set_child(self.event_list)
        page.append(event_scroll)
        self.inspector_stack.add_titled(page, "activity", "Activity")
        self.event_rows_signature = None

    def _render_event_feed(self):
        events = list(self.activity.events)[-40:]
        signature = tuple((event.kind, event.at, event.connection.socket_key) for event in events)
        if signature == self.event_rows_signature:
            return
        self.event_rows_signature = signature
        while child := self.event_list.get_first_child():
            self.event_list.remove(child)
        self.event_empty.set_visible(not events)
        for event in reversed(events):
            connection = event.connection
            process_name = connection.process.name if connection.process else "Unattributed"
            opened = event.kind == "opened"
            row = Gtk.ListBoxRow()
            row.add_css_class("event-row")
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
            line.set_margin_top(7)
            line.set_margin_bottom(7)
            line.set_margin_start(8)
            line.set_margin_end(8)
            row.set_child(line)
            badge = _label("OPEN" if opened else "CLOSED", "event-open" if opened else "event-closed")
            line.append(badge)
            text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text_box.set_hexpand(True)
            line.append(text_box)
            text_box.append(_label(f"{process_name}  →  {connection.remote}", "flow-title"))
            text_box.append(_label(f"{connection.protocol} · {connection.state}", "muted-small"))
            clock = _label(datetime.fromtimestamp(event.at).strftime("%H:%M:%S"), "event-time")
            clock.set_tooltip_text(datetime.fromtimestamp(event.at).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
            line.append(clock)
            self.event_list.append(row)

    def _build_footnote(self, parent):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        parent.append(row)
        self.visibility_note = _label("LOCAL ONLY  ·  NO PACKET CAPTURE  ·  NO REPUTATION LOOKUPS", "muted-small")
        self.visibility_note.set_hexpand(True)
        row.append(self.visibility_note)
        row.append(_label(f"NetAtlas {__version__}", "muted-small"))

    def _set_initial_splits(self):
        self._resize_map_split(self.layout_paned, None)
        width = self.activity_paned.get_width()
        if width > 0:
            self.activity_paned.set_position(int(width * .68))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _resize_map_split(paned, _param):
        height = paned.get_height()
        if height > 0:
            # Keep the inspection dock compact; let the atlas absorb extra space.
            paned.set_position(max(340, height - 260))

    def _on_fullscreen(self, button):
        if button.get_active():
            self.fullscreen()
        else:
            self.unfullscreen()

    def _on_refresh_interval(self, dropdown, _pspec):
        self.refresh_ms = (2000, 5000, 10000)[dropdown.get_selected()]
        if hasattr(self, "poll_source"):
            GLib.source_remove(self.poll_source)
            self.poll_source = GLib.timeout_add(self.refresh_ms, self._tick)

    def _on_sort_changed(self, *_):
        self.sort_mode = ("Most sockets", "Recently changed", "App name")[self.sort_dropdown.get_selected()]
        self.list_limit = MAX_LIST_ROWS
        self._refresh_list()

    def _on_load_more(self, *_):
        self.list_limit += MAX_LIST_ROWS
        self._refresh_list()

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_F11:
            self.fullscreen_button.set_active(not self.fullscreen_button.get_active())
            return True
        if keyval == Gdk.KEY_Escape and self.fullscreen_button.get_active():
            self.fullscreen_button.set_active(False)
            return True
        return False

    def _animate(self):
        if not self.paused and self.graph_connections and self.get_mapped():
            self.animation_phase = (self.animation_phase + 4.0) % 280.0
            self.canvas.queue_draw()
        return GLib.SOURCE_CONTINUE

    @classmethod
    def _aggregate_graph(cls, connections):
        proc_counts = Counter()
        endpoint_counts = Counter()
        process_pids = {}
        edge_counts = Counter()
        edge_scopes = {}
        for connection in connections:
            pkey = cls._process_key(connection)
            proc_counts[pkey] += 1
            endpoint_counts[connection.remote] += 1
            if connection.process:
                process_pids.setdefault(pkey, set()).add(connection.process.pid)
            pair = (pkey, connection.remote)
            edge_counts[pair] += 1
            edge_scopes[pair] = endpoint_scope(connection)
        return proc_counts, endpoint_counts, process_pids, edge_counts, edge_scopes

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

        connections = self.graph_connections
        if not connections:
            self._draw_text(cr, "No visible active connections", cx, cy - 4, 15, (0.66, .74, .82), True)
            self._draw_text(cr, "Start an app that uses the network to populate the atlas", cx, cy + 22, 11, (.43, .52, .61))
            self._draw_text(cr, "LOCAL PROCESSES", 28, 32, 10, (.58, .72, .81), True, align="left")
            self._draw_text(cr, "REMOTE ENDPOINTS", width - 28, 32, 10, (.58, .72, .81), True, align="right")
            return

        proc_counts, endpoint_counts, process_pids, edge_counts, edge_scopes = self.graph_model

        max_processes = max(8, min(18, int(height / 27)))
        max_endpoints = max(10, min(22, int(height / 21)))
        procs = [key for key, _ in proc_counts.most_common(max_processes)]
        endpoints = [key for key, _ in endpoint_counts.most_common(max_endpoints)]
        proc_positions = {p: (width * .29, height * (i + 1) / (len(procs) + 1))
                          for i, p in enumerate(procs)}
        end_positions = {endpoint: (width * .77, height * (i + 1) / (len(endpoints) + 1))
                         for i, endpoint in enumerate(endpoints)}

        visible_edges = [pair for pair, _ in edge_counts.most_common(MAX_MAP_EDGES)
                         if pair[0] in proc_positions and pair[1] in end_positions]
        selected_pair = ((self._process_key(self.selected), self.selected.remote)
                         if self.selected else None)
        if selected_pair and selected_pair in edge_counts and selected_pair not in visible_edges:
            visible_edges.append(selected_pair)

        self._draw_text(cr, "APPLICATIONS", 28, 28, 10, (.58, .72, .81), True, align="left")
        self._draw_text(cr, "DESTINATIONS", width - 28, 28, 10, (.58, .72, .81), True, align="right")
        for index, pair in enumerate(visible_edges):
            pkey, endpoint = pair
            x1, y1 = proc_positions[pkey]
            x2, y2 = end_positions[endpoint]
            selected = pair == selected_pair
            scope = edge_scopes[pair]
            base = (0.98, 0.42, 0.21) if scope == "PUBLIC" else (0.24, 0.77, 0.69)
            color = (0.76, 0.52, 1.0) if selected else base
            p0 = (x1 + 9, y1)
            p1 = (cx - width * .06, y1)
            p2 = (cx + width * .05, y2)
            p3 = (x2 - 9, y2)
            if selected:
                cr.set_source_rgba(.66, .37, 1.0, .18)
                cr.set_line_width(10)
                cr.move_to(*p0)
                cr.curve_to(*p1, *p2, *p3)
                cr.stroke()
            cr.set_source_rgba(*color, .86 if selected else .43)
            cr.set_line_width((2.8 if selected else 1.1) + min(1.8, math.log2(edge_counts[pair]) * .3))
            cr.set_dash([6.0, 8.0], -self.animation_phase)
            cr.move_to(*p0)
            cr.curve_to(*p1, *p2, *p3)
            cr.stroke()
            cr.set_dash([], 0)

            # Arrow and traveling pulse both follow the app-to-destination path.
            t = ((self.animation_phase / 280.0) + index * .137) % 1.0
            point = self._bezier(p0, p1, p2, p3, t)
            pulse_radius = 5.2 if selected else 3.1
            cr.set_source_rgba(*color, .35)
            cr.arc(*point, pulse_radius * 2.5, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(*color)
            cr.arc(*point, pulse_radius, 0, math.tau)
            cr.fill()
            arrow_point = self._bezier(p0, p1, p2, p3, .94)
            before = self._bezier(p0, p1, p2, p3, .91)
            angle = math.atan2(arrow_point[1] - before[1], arrow_point[0] - before[0])
            cr.move_to(*arrow_point)
            cr.line_to(arrow_point[0] - 8 * math.cos(angle - .52), arrow_point[1] - 8 * math.sin(angle - .52))
            cr.line_to(arrow_point[0] - 8 * math.cos(angle + .52), arrow_point[1] - 8 * math.sin(angle + .52))
            cr.close_path()
            cr.fill()

        # The device hub sits above the wires, anchoring the map without
        # implying that the graph represents geography or packet volume.
        cr.set_source_rgba(.21, .78, .73, .13)
        cr.arc(cx, cy, 33, 0, math.tau)
        cr.fill()
        cr.set_source_rgb(.32, .91, .83)
        cr.arc(cx, cy, 16, 0, math.tau)
        cr.fill()
        self._draw_text(cr, "LOCAL", cx, cy + 3, 7, (.04, .10, .12), True)

        for key, (x, y) in proc_positions.items():
            selected_node = bool(selected_pair and selected_pair[0] == key)
            node_color = (.76, .52, 1.0) if selected_node else (.34, .86, .79)
            cr.set_source_rgba(*node_color, .18)
            cr.arc(x, y, 15 if not selected_node else 19, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(*node_color)
            cr.arc(x, y, 5.5 if selected_node else 4.5, 0, math.tau)
            cr.fill()
            name = key[1]
            if len(process_pids.get(key, ())) > 1:
                name += f"  ×{len(process_pids[key])}"
            self._draw_text(cr, name[:25], x - 22, y + 4, 12,
                            (.94, .96, 1.0) if selected_node else (.78, .85, .93),
                            selected_node, align="right")
        for endpoint in endpoints:
            x, y = end_positions[endpoint]
            selected_node = bool(selected_pair and selected_pair[1] == endpoint)
            try:
                ip = ipaddress.ip_address(endpoint.rsplit(":", 1)[0].strip("[]"))
                public = ip.is_global
            except ValueError:
                public = False
            node_color = (.76, .52, 1.0) if selected_node else ((1.0, .48, .25) if public else (.30, .79, .70))
            cr.set_source_rgba(*node_color, .17)
            cr.arc(x, y, 14 if not selected_node else 18, 0, math.tau)
            cr.fill()
            cr.set_source_rgb(*node_color)
            cr.arc(x, y, 5.5 if selected_node else 4.5, 0, math.tau)
            cr.fill()
            label = endpoint if len(endpoint) <= 34 else endpoint[:31] + "…"
            self._draw_text(cr, label, x + 22, y + 4, 10,
                            (.94, .96, 1.0) if selected_node else (.72, .79, .86),
                            selected_node, align="left")
        if len(proc_counts) > len(procs) or len(endpoint_counts) > len(endpoints) or len(edge_counts) > len(visible_edges):
            self._draw_text(cr, f"Top {len(procs)} apps · {len(endpoints)} destinations · {len(visible_edges)} strongest routes · {len(connections):,} sockets below",
                            width / 2, height - 13, 10, (.59, .68, .77))

    @staticmethod
    def _process_key(connection):
        process = connection.process
        return ((process.exe or process.name, process.name)
                if process else ("", "Unattributed"))

    @staticmethod
    def _bezier(p0, p1, p2, p3, t):
        u = 1.0 - t
        return (u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1])

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
                started = time.monotonic()
                snapshot = read_snapshot()
                elapsed = time.monotonic() - started
                GLib.idle_add(self._receive_snapshot, snapshot, None, elapsed)
            except Exception as exc:  # Keep the UI alive if procfs is restricted.
                GLib.idle_add(self._receive_snapshot, None, str(exc), 0.0)
        threading.Thread(target=worker, name="netatlas-procfs", daemon=True).start()

    def _receive_snapshot(self, snapshot, error, elapsed=0.0):
        self.busy = False
        if error:
            self.live_pill.set_text("  MONITOR ERROR  ")
            self.map_hint.set_text(error)
            return GLib.SOURCE_REMOVE
        self.snapshot = snapshot
        cutoff = snapshot.timestamp - HISTORY_SECONDS
        self.activity.update(snapshot)
        recent_events = sum(event.kind == "opened" and event.at >= cutoff
                            for event in self.activity.events)
        self.live_pill.set_text("  DEMO DATA · SYNTHETIC  " if DEMO_MODE else "  LIVE · PROCFS  ")
        self.map_hint.set_text(f"Updated just now · scan {elapsed:.2f}s · {snapshot.hidden_process_count} unattributed")
        self.map_hint.set_tooltip_text(
            f"Best-effort process ownership. Some sockets can be unattributed because of /proc restrictions, races, or kernel ownership. Last scan: {elapsed:.2f} seconds."
        )
        connections = tuple(self.activity.connections.values())
        self.graph_connections = connections
        self.graph_model = self._aggregate_graph(connections)
        self.metric_labels["connections"].set_text(str(len(connections)))
        self.metric_labels["apps"].set_text(str(len({c.process.pid for c in connections if c.process})))
        self.metric_labels["destinations"].set_text(str(len({c.remote for c in connections})))
        self.metric_labels["new"].set_text(str(recent_events))
        self.connection_count.set_text(f"{len(connections)} currently visible · {snapshot.hidden_process_count} unattributed")
        signature = frozenset((connection.key, connection.state) for connection in connections)
        if signature != self.list_signature:
            self.list_signature = signature
            self._refresh_list()
        self._render_event_feed()
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
        self.list_limit = MAX_LIST_ROWS
        self._refresh_list()

    def _refresh_list(self):
        selected_pair = ((self._process_key(self.selected), self.selected.remote)
                         if self.selected else None)
        self.rebuilding_list = True
        while child := self.flow_list.get_first_child():
            self.flow_list.remove(child)
        groups = {}
        for connection in self.activity.connections.values():
            pkey = self._process_key(connection)
            group_key = (pkey, connection.remote, connection.protocol, connection.state)
            groups.setdefault(group_key, []).append(connection)
        if self.search_text:
            groups = {
                key: group for key, group in groups.items()
                if self.search_text in (
                    f"{key[0][1]} {key[1]} {key[2]} {key[3]} "
                    + " ".join(str(c.process.pid) for c in group if c.process)
                    + " " + " ".join(c.local for c in group)
                ).casefold()
            }
        last_changed = {}
        for event in self.activity.events:
            last_changed[event.connection.socket_key] = event.at
        def sort_key(item):
            key, group = item
            changed = max((last_changed.get(connection.socket_key, 0) for connection in group), default=0)
            stable = (key[0][1].casefold(), key[1], key[2], key[3])
            if self.sort_mode == "Recently changed":
                return (-changed, -len(group), *stable)
            if self.sort_mode == "App name":
                return stable
            return (-len(group), -changed, *stable)
        group_rows = sorted(groups.items(), key=sort_key)
        total_matches = sum(len(group) for group in groups.values())
        shown = 0
        selected_row = None
        for (pkey, remote, protocol, state), group in group_rows[:self.list_limit]:
            connection = group[0]
            process = connection.process
            proc_name = pkey[1]
            row = Gtk.ListBoxRow()
            row.connection = connection
            row.connections = group
            row.add_css_class("flow-row")
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
            main.append(_label(f"{proc_name}  →  {remote}", "flow-title"))
            pids = {c.process.pid for c in group if c.process}
            owner_text = f"{len(pids)} process(es)" if len(pids) > 1 else (f"PID {process.pid}" if process else "Process details unavailable")
            main.append(_label(f"{len(group)} socket(s) · {protocol} · {state} · {owner_text}", "muted-small"))
            scope = _label(endpoint_scope(connection), "scope-public" if endpoint_scope(connection) == "PUBLIC" else "scope-local")
            line.append(scope)
            self.flow_list.append(row)
            shown += 1
            if selected_pair and (pkey, remote) == selected_pair:
                selected_row = row
        self.empty.set_visible(shown == 0)
        self.empty.set_text(
            "No connections match this filter." if self.search_text else
            "No active internet sockets visible yet. Start an app that connects to the network."
        )
        if selected_row is None and DEMO_MODE and self.flow_list.get_row_at_index(0):
            selected_row = self.flow_list.get_row_at_index(0)
        more = max(0, len(group_rows) - shown)
        self.load_more.set_visible(more > 0)
        self.load_more.set_label(f"Load next {min(more, MAX_LIST_ROWS)} groups · {more:,} remaining")
        if len(group_rows) > shown:
            self.connection_count.set_text(
                f"Showing {shown:,} of {len(group_rows):,} groups · {total_matches:,} sockets"
            )
        elif self.search_text:
            self.connection_count.set_text(f"{len(groups):,} groups · {total_matches:,} sockets")
        self.flow_list.select_row(selected_row)
        self.selected = selected_row.connection if selected_row else None
        self.selected_group_count = len(selected_row.connections) if selected_row else 1
        self.rebuilding_list = False
        self._show_detail(self.selected, self.selected_group_count)

    def _on_row_selected(self, _list, row):
        if self.rebuilding_list:
            return
        self.selected = row.connection if row else None
        self.selected_group_count = len(row.connections) if row else 1
        self._show_detail(self.selected, self.selected_group_count)
        self.canvas.queue_draw()

    def _show_detail(self, connection, group_count=1):
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
                  ("Matching sockets", str(group_count)),
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
