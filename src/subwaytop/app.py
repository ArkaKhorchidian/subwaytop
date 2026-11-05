"""The Textual app: live map, line legend, alerts and station boards."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Static

from . import model
from .canvas import Canvas, View, fit, mix

LINE_KEYS = list("1234567ACEBDFMGJZLNQRW") + ["S"]
MUTED = "#7a8494"


def bullet(route: str) -> Text:
    r = model.network()["routes"]
    d = model.display_route(route)
    info = r.get(route) or r.get(d) or {"color": "#808183", "text": "#FFFFFF"}
    label = "SIR" if d == "SI" else d
    t = Text()
    t.append(f" {label} ", style=f"bold {info['text']} on {info['color']}")  # styled span, not a base style
    return t


class MapView(Static):
    """Renders tracks once per view; trains are redrawn on top every frame."""

    def __init__(self, app_ref: "SubwayApp", **kw):
        super().__init__(**kw)
        self.app_ref = app_ref
        self._tracks_key = None
        self._tracks: Canvas | None = None

    def tracks(self, w: int, h: int, view: View, focus: str | None) -> Canvas:
        key = (w, h, round(view.lat, 6), round(view.lon, 6), round(view.scale, 3), focus)
        if key == self._tracks_key and self._tracks is not None:
            return self._tracks
        c = Canvas(w, h)
        net = model.network()
        for route, polys in net["lines"].items():
            d = model.display_route(route)
            color = net["routes"][route]["color"]
            strength = 0.8 if focus is None else (1.0 if d == focus else 0.15)
            dim = mix(color, strength)
            prio = 2 if focus and d == focus else 1
            for poly in polys:
                pts = [view.project(lat, lon, w, h) for lat, lon in poly]
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    c.line(x0, y0, x1, y1, dim, prio)
                    if view.scale > 1500:  # thicker track when zoomed in
                        c.line(x0 + 1, y0, x1 + 1, y1, dim, prio)
        self._tracks_key, self._tracks = key, c
        return c

    def frame(self, trains: list[model.TrainPos], view: View, focus: str | None, station: dict | None) -> Text:
        w, h = max(10, self.size.width), max(5, self.size.height)
        base = self.tracks(w, h, view, focus)
        c = Canvas(w, h)
        c.bits, c.color, c.prio = list(base.bits), list(base.color), list(base.prio)
        net = model.network()
        for t in trains:
            d = model.display_route(t.route)
            if focus and d != focus:
                continue
            info = net["routes"].get(t.route) or net["routes"].get(d)
            x, y = view.project(t.lat, t.lon, w, h)
            c.put(x // 2, y // 4, "●", f"bold {info['color']}")
        if station:
            x, y = view.project(station["lat"], station["lon"], w, h)
            c.put(x // 2, y // 4, "◎", "bold #ffffff on #e5534b")
        # station labels when zoomed in, busiest stations first
        if view.scale > 2600:
            for g in sorted(model.complexes(), key=lambda g: -len(g["routes"])):
                x, y = view.project(g["lat"], g["lon"], w, h)
                cx, cy = x // 2, y // 4
                name = g["name"].split("-")[0][:18]
                if c.free(cx, cy, len(name) + 2):
                    c.put(cx, cy, "◦", "#c9d1d9")
                    c.text(cx + 1, cy, " " + name, "#8b949e")
        return c.render()


class SubwayApp(App):
    TITLE = "subwaytop"
    CSS = """
    Screen { background: #0d1117; }
    #map { width: 1fr; height: 1fr; }
    #side { width: 44; border-left: tall #21262d; padding: 0 1; }
    #head { height: auto; }
    #legend { height: auto; margin: 1 0 0 0; }
    #panel { height: 1fr; margin: 1 0 0 0; }
    #search { dock: bottom; display: none; }
    Footer { background: #161b22; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Station"),
        Binding("plus,equals_sign", "zoom(1.5)", "Zoom in"),
        Binding("minus", "zoom(0.667)", "Zoom out"),
        Binding("up,k", "pan(0,-1)", "Pan", show=False),
        Binding("down,j", "pan(0,1)", "Pan", show=False),
        Binding("left,h", "pan(-1,0)", "Pan", show=False),
        Binding("right,l", "pan(1,0)", "Pan", show=False),
        Binding("escape,0", "reset", "Reset"),
        Binding("space", "pause", "Pause", show=False),
        Binding("right_square_bracket", "speed(2)", "Faster", show=False),
        Binding("left_square_bracket", "speed(0.5)", "Slower", show=False),
    ] + [Binding(k, f"focus('{k}')", "Line", show=False) for k in LINE_KEYS]

    def __init__(self, source: Callable[[], model.Snapshot] | None = None, replay: list[model.Snapshot] | None = None,
                 speed: float = 20.0, focus: str | None = None, station: str | None = None,
                 exit_after: float | None = None, refresh: float = 30.0, zoom: float = 1.0):
        super().__init__()
        self.start_zoom = zoom
        self.source, self.replay_snaps, self.speed = source, replay, speed
        self.focus_line = focus
        self.station_query = station
        self.station: dict | None = None
        self.exit_after, self.refresh_every = exit_after, refresh
        self.snap: model.Snapshot | None = replay[0] if replay else None
        self.view: View | None = None
        self.paused = False
        self.status = "loading…"
        self._clock_start = time.monotonic()
        self._sim = replay[0].taken if replay else time.time()

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield MapView(self, id="map")
            with Vertical(id="side"):
                yield Static(id="head")
                yield Static(id="legend")
                yield Static(id="panel")
        yield Input(placeholder="Station name, e.g. Union Sq, Atlantic Av, 125 St", id="search")
        yield Footer()

    def on_mount(self) -> None:
        box = self.query_one("#search", Input)
        box.can_focus = False  # hidden until `/`, so it must not swallow keys meant for the map
        self.set_focus(None)
        if self.source:
            self.run_worker(self._poll, thread=True, exclusive=True)
        if self.station_query:
            self._select(self.station_query)
        self.set_interval(1 / 8, self._tick)
        if self.exit_after:
            self.set_timer(self.exit_after, self.exit)

    # -- data ------------------------------------------------------------------
    def _poll(self) -> None:
        while True:
            try:
                snap = self.source()
                self.call_from_thread(self._got, snap, None)
            except Exception as e:  # network trouble: keep showing the last good data
                self.call_from_thread(self._got, None, str(e))
            time.sleep(self.refresh_every)

    def _got(self, snap, err) -> None:
        if snap:
            self.snap = snap
            self.status = f"live · {snap.feeds_ok}/{len(model.FEEDS)} feeds"
        else:
            self.status = f"offline: {err[:30]}" if not self.snap else "reconnecting…"

    def now(self) -> float:
        if not self.replay_snaps:
            return time.time()
        return self._sim

    def _tick(self) -> None:
        if self.replay_snaps and not self.paused:
            self._sim += self.speed / 8
            snaps = self.replay_snaps
            if self._sim > snaps[-1].taken + 30:
                self._sim = snaps[0].taken  # loop the recording
            self.snap = max((s for s in snaps if s.taken <= self._sim), key=lambda s: s.taken, default=snaps[0])
            self.status = f"replay ×{self.speed:g}" + (" · paused" if self.paused else "")
        self._render()

    # -- drawing -----------------------------------------------------------------
    def _render(self) -> None:
        mp = self.query_one("#map", MapView)
        if mp.size.width < 10:
            return
        if self.view is None:
            pts = [(s[1], s[2]) for s in model.network()["stations"].values() if "SI" not in s[3]]
            self.view = fit(pts, mp.size.width, mp.size.height)
            self._home = View(self.view.lat, self.view.lon, self.view.scale)
            if self.start_zoom != 1.0:  # zoom toward Midtown/Downtown, where most lines meet
                self.view = View(40.735, -73.975, self.view.scale * self.start_zoom)
            if self.station:
                self._center_on(self.station)
        now = self.now()
        trains = model.positions(self.snap, now) if self.snap else []
        mp.update(mp.frame(trains, self.view, self.focus_line, self.station))
        self.query_one("#head", Static).update(self._head(trains, now))
        self.query_one("#legend", Static).update(self._legend(trains, now))
        self.query_one("#panel", Static).update(self._board(now) if self.station else self._alerts(now))

    def _head(self, trains, now) -> Text:
        t = Text()
        t.append(" subwaytop ", style="bold #0d1117 on #ffcc00")
        live = self.status.startswith("live")
        t.append(f"  {'●' if live else '○'} {self.status}", style="#3fb950" if live else "#d29922")
        t.append(f"\n {datetime.fromtimestamp(now):%a %H:%M:%S}", style="bold")
        t.append(f" · {len(trains)} trains running", style=MUTED)
        return t

    def _legend(self, trains, now) -> Group:
        counts: dict[str, int] = {}
        for tr in trains:
            d = model.display_route(tr.route)
            counts[d] = counts.get(d, 0) + 1
        alerted = {model.display_route(r) for a in model.active_alerts(self.snap.alerts if self.snap else [], now)
                   if a["type"] in ("Delays", "Part Suspended", "Suspended") for r in a["routes"]}
        rows, row = [], Text()
        for i, key in enumerate(LINE_KEYS + ["SI"]):
            cell = bullet(key)
            n = counts.get(key, 0)
            style = "bold #ff7b72" if key in alerted else ("#c9d1d9" if n else MUTED)
            cell.append(f"{n:>3}" + ("!" if key in alerted else " "), style=style)
            if self.focus_line and key != self.focus_line:
                cell.stylize("dim")
            row.append_text(cell)
            row.append(" ")
            if (i + 1) % 4 == 0:
                rows.append(row)
                row = Text()
        if row:
            rows.append(row)
        rows.append(Text("! delays · press a line to focus it", style=MUTED))
        return Group(*rows)

    def _alerts(self, now) -> Group:
        al = model.active_alerts(self.snap.alerts if self.snap else [], now)
        if self.focus_line:
            al = [a for a in al if any(model.display_route(r) == self.focus_line for r in a["routes"])]
        order = {"Delays": 0, "Suspended": 1, "Part Suspended": 1}
        al.sort(key=lambda a: order.get(a["type"], 5))
        parts = [Text(f"SERVICE ALERTS  {len(al)}", style="bold")]
        for a in al[:12]:
            t = Text()
            for r in a["routes"][:4]:
                t.append_text(bullet(r))
                t.append(" ")
            t.append(a["type"] or "Alert", style="bold #ff7b72" if a["type"] == "Delays" else "bold #d29922")
            t.append("\n" + a["header"][:180], style="#c9d1d9")
            parts += [Text(""), t]
        if not al:
            parts.append(Text("Good service.", style="#3fb950"))
        parts.append(Text("\n/ to open a station's departures", style=MUTED))
        return Group(*parts)

    def _board(self, now) -> Group:
        st = self.station
        head = Text(st["name"], style="bold")
        head.append("\n")
        for r in dict.fromkeys(model.display_route(r) for r in st["routes"]):
            head.append_text(bullet(r))
            head.append(" ")
        parts = [head]
        b = model.board(self.snap, st, now) if self.snap else {"N": [], "S": []}
        for d, label in (("N", "NORTHBOUND / UPTOWN"), ("S", "SOUTHBOUND / DOWNTOWN")):
            parts.append(Text(f"\n{label}", style=f"bold {MUTED}"))
            if not b[d]:
                parts.append(Text("no trains due", style=MUTED))
            for dep in b[d][:6]:
                t = bullet(dep.route)
                t.append(f" {dep.destination[:24]:<24}", style="#c9d1d9")
                m = dep.minutes
                t.append(f"{'now' if m < 0.75 else f'{m:.0f} min':>7}", style="bold #3fb950" if m < 0.75 else "bold")
                parts.append(t)
        parts.append(Text("\nesc to go back", style=MUTED))
        return Group(*parts)

    # -- actions -----------------------------------------------------------------
    def action_zoom(self, f: float) -> None:
        if self.view:
            self.view.scale = max(200.0, min(self.view.scale * f, 40000.0))

    def action_pan(self, dx: int, dy: int) -> None:
        if self.view:
            mp = self.query_one("#map")
            step = 0.25 * min(mp.size.width * 2, mp.size.height * 4)
            lat, lon = self.view.unproject(mp.size.width + dx * step, 2 * mp.size.height + dy * step,
                                           mp.size.width, mp.size.height)
            self.view.lat, self.view.lon = lat, lon

    def action_reset(self) -> None:
        if self.query_one("#search", Input).display:  # esc while searching just closes the box
            self._close_search()
            return
        self.station = None
        self.focus_line = None
        if self.view and hasattr(self, "_home"):
            self.view = View(self._home.lat, self._home.lon, self._home.scale)

    def action_focus(self, line: str) -> None:
        self.focus_line = None if self.focus_line == line else line

    def action_pause(self) -> None:
        self.paused = not self.paused

    def action_speed(self, f: float) -> None:
        self.speed = max(1.0, min(self.speed * f, 240.0))

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.can_focus = True
        box.display = True
        box.value = ""
        box.focus()

    def _close_search(self) -> None:
        box = self.query_one("#search", Input)
        box.display = False
        box.can_focus = False
        self.set_focus(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._close_search()
        if not self._select(event.value):
            self.notify(f"No station matches “{event.value}”", severity="warning")

    def _select(self, query: str) -> bool:
        found = model.find_station(query)
        if not found:
            return False
        self.station = found[0]
        if self.view:
            self._center_on(self.station)
        return True

    def _center_on(self, st: dict) -> None:
        self.view.lat, self.view.lon = st["lat"], st["lon"]
        self.view.scale = max(self.view.scale, 3200.0)
