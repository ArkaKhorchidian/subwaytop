"""The subway network, live train positions, departure boards and alerts."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .gtfsrt import Feed, Trip, parse

BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
FEEDS = ["gtfs", "gtfs-ace", "gtfs-bdfm", "gtfs-g", "gtfs-jz", "gtfs-nqrw", "gtfs-l", "gtfs-si"]
ALERTS = "camsys%2Fsubway-alerts.json"
STOPPED_AT = 1


@lru_cache(maxsize=1)
def network() -> dict:
    with resources.files(__package__).joinpath("data/nyc.json").open("r") as fh:
        return json.load(fh)


def display_route(route: str) -> str:
    """Express variants share the local's bullet (6X -> 6); shuttles are S."""
    if route in ("GS", "FS", "H"):
        return "S"
    return route[:-1] if len(route) == 2 and route.endswith("X") else route


def station_of(stop_id: str) -> str:
    return stop_id[:-1] if stop_id[-1:] in ("N", "S") else stop_id


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _get(url: str, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "subwaytop"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_all() -> tuple[dict[str, bytes], bytes | None]:
    with ThreadPoolExecutor(max_workers=len(FEEDS) + 1) as ex:
        futures = {f: ex.submit(_get, BASE + "nyct%2F" + f) for f in FEEDS}
        alerts = ex.submit(_get, BASE + ALERTS)
        raw = {}
        for f, fut in futures.items():
            try:
                raw[f] = fut.result()
            except Exception:  # one feed down shouldn't blank the map
                pass
        try:
            al = alerts.result()
        except Exception:
            al = None
    return raw, al


@dataclass
class Snapshot:
    taken: float
    trips: list[Trip]
    alerts: list[dict]
    feeds_ok: int


def build_snapshot(raw: dict[str, bytes], alerts_raw: bytes | None, taken: float | None = None) -> Snapshot:
    trips: list[Trip] = []
    newest = 0
    for data in raw.values():
        try:
            f: Feed = parse(data)
        except (ValueError, IndexError):
            continue
        newest = max(newest, f.timestamp)
        trips += [t for t in f.trips.values() if t.route_id]
    alerts = parse_alerts(alerts_raw) if alerts_raw else []
    return Snapshot(taken or newest or time.time(), trips, alerts, len(raw))


def parse_alerts(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out = []
    for e in data.get("entity", []):
        a = e.get("alert") or {}
        routes = sorted({ie.get("route_id") for ie in a.get("informed_entity", []) if ie.get("route_id")})
        header = next((t.get("text", "") for t in (a.get("header_text") or {}).get("translation", [])
                       if t.get("language", "en") == "en"), "")
        periods = [(p.get("start", 0), p.get("end") or 0) for p in a.get("active_period", [])]
        kind = (a.get("transit_realtime.mercury_alert") or {}).get("alert_type", "")
        if routes and header:
            out.append({"routes": routes, "header": re.sub(r"\s+", " ", header).strip(), "periods": periods,
                        "type": kind})
    return out


def active_alerts(alerts: list[dict], now: float) -> list[dict]:
    def live(a):
        return not a["periods"] or any(s <= now and (e == 0 or now <= e) for s, e in a["periods"])
    return [a for a in alerts if live(a)]


# ---------------------------------------------------------------------------
# Where is every train?
# ---------------------------------------------------------------------------
@dataclass
class TrainPos:
    route: str
    direction: str  # "N" or "S"
    lat: float
    lon: float
    at_station: bool
    next_stop: str
    trip_id: str


def _coords(stop_id: str) -> tuple[float, float] | None:
    s = network()["stations"].get(station_of(stop_id))
    return (s[1], s[2]) if s else None


DWELL = 25  # seconds a train typically sits at a station


def position(t: Trip, now: float) -> TrainPos | None:
    """Place a train using its predicted arrival times; the feed's "stopped at" flag is often stale."""
    stops = [s for s in t.stops if s.time]
    if not stops:
        return None
    direction = stops[0].stop_id[-1:] if stops[0].stop_id[-1:] in ("N", "S") else "N"
    if stops[0].time - now > 300:  # next departure is a scheduled trip, not a train on the tracks yet
        return None

    def at(stop_id: str) -> TrainPos | None:
        c = _coords(stop_id)
        return TrainPos(t.route_id, direction, c[0], c[1], True, stop_id, t.trip_id) if c else None

    idx = next((i for i, s in enumerate(stops) if s.time > now), None)
    if idx is None:  # every prediction is in the past: arriving at, or just finished at, the last stop
        return at(stops[-1].stop_id) if now - stops[-1].time < 90 else None
    nxt = stops[idx]
    if t.status == STOPPED_AT and t.at_stop == nxt.stop_id:  # arrived a little ahead of the prediction
        return at(nxt.stop_id)
    if idx > 0:
        prv_id, t0 = stops[idx - 1].stop_id, stops[idx - 1].time + DWELL
    else:
        link = network()["prev"].get(t.route_id, {}).get(nxt.stop_id)
        if not link:  # waiting at its starting terminal
            return at(nxt.stop_id) if t.assigned else None
        if not t.assigned and nxt.time - now > link[1]:  # scheduled, no train yet
            return None
        prv_id, t0 = link[0], nxt.time - link[1]
    if now <= t0:
        return at(prv_id)
    a, b = _coords(prv_id), _coords(nxt.stop_id)
    if not a or not b:
        return None
    f = min(1.0, (now - t0) / max(1.0, nxt.time - t0))
    return TrainPos(t.route_id, direction, a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
                    False, nxt.stop_id, t.trip_id)


def positions(snap: Snapshot, now: float) -> list[TrainPos]:
    out = []
    for t in snap.trips:
        p = position(t, now)
        if p:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Stations and departure boards
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def complexes() -> list[dict]:
    """Stations grouped as riders think of them: same name within ~400 m (e.g. Times Sq's four)."""
    groups: list[dict] = []
    for sid, (name, lat, lon, routes) in sorted(network()["stations"].items()):
        for g in groups:
            if g["name"] == name and abs(g["lat"] - lat) < 0.004 and abs(g["lon"] - lon) < 0.005:
                g["ids"].append(sid)
                g["routes"] = sorted(set(g["routes"]) | set(routes), key=route_order)
                break
        else:
            groups.append({"name": name, "lat": lat, "lon": lon, "ids": [sid], "routes": sorted(routes, key=route_order)})
    return groups


def route_order(r: str) -> tuple:
    order = "1234567ACEBDFMGJZLNQRWS"
    d = display_route(r)
    return (order.index(d) if d in order else 99, r)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("street", "st").replace("avenue", "av").replace("square", "sq"))


def find_station(query: str) -> list[dict]:
    q = _norm(query)
    if not q:
        return []
    exact = [g for g in complexes() if _norm(g["name"]) == q]
    if exact:
        return exact
    # shorter names and bigger hubs first: "bedford" -> Bedford Av, "atlantic" -> Atlantic Av-Barclays Ctr
    by_len = sorted(complexes(), key=lambda g: len(g["name"]) - 6 * len({display_route(r) for r in g["routes"]}))
    starts = [g for g in by_len if _norm(g["name"]).startswith(q)]
    contains = [g for g in by_len if q in _norm(g["name"]) and g not in starts]
    return starts + contains


@dataclass
class Departure:
    route: str
    direction: str
    minutes: float
    destination: str


def board(snap: Snapshot, station: dict, now: float, limit: int = 8) -> dict[str, list[Departure]]:
    ids = set(station["ids"])
    names = network()["stations"]
    out: dict[str, list[Departure]] = {"N": [], "S": []}
    for t in snap.trips:
        stops = [s for s in t.stops if s.time]
        for i, s in enumerate(stops):
            if station_of(s.stop_id) in ids and s.time >= now - 20:
                dest = names.get(station_of(stops[-1].stop_id), ["?"])[0]
                if station_of(stops[-1].stop_id) in ids:  # terminates here
                    break
                d = s.stop_id[-1:] if s.stop_id[-1:] in ("N", "S") else "N"
                out[d].append(Departure(t.route_id, d, max(0.0, (s.time - now) / 60), dest))
                break
    for d in out:
        out[d] = sorted(out[d], key=lambda x: x.minutes)[:limit]
    return out


# ---------------------------------------------------------------------------
# Recording and replay
# ---------------------------------------------------------------------------
def save_snapshot(directory: Path, raw: dict[str, bytes], alerts: bytes | None, taken: float) -> Path:
    d = directory / f"snap_{int(taken)}"
    d.mkdir(parents=True, exist_ok=True)
    for name, data in raw.items():
        (d / f"{name}.pb").write_bytes(data)
    if alerts:
        (d / "alerts.json").write_bytes(alerts)
    return d


def load_recording(directory: Path) -> list[Snapshot]:
    snaps = []
    for d in sorted(Path(directory).glob("snap_*")):
        raw = {p.stem: p.read_bytes() for p in d.glob("*.pb") if p.stat().st_size > 0}
        al = (d / "alerts.json").read_bytes() if (d / "alerts.json").exists() else None
        if raw:
            snaps.append(build_snapshot(raw, al, taken=float(d.name.split("_")[1])))
    return snaps


def distance_m(lat1, lon1, lat2, lon2) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * 111_320, (lon2 - lon1) * 111_320 * k)
