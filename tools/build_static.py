"""Condense the MTA's static GTFS into the small JSON file subwaytop ships with.

    python tools/build_static.py google_transit.zip src/subwaytop/data/nyc.json

Source: http://web.mta.info/developers/data/nyct/subway/google_transit.zip (MTA, free to use).
"""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
import sys
import zipfile
from collections import Counter, defaultdict


def rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def secs(t: str) -> int:
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def simplify(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on (lat, lon) using a local metric scale."""
    if len(pts) < 3:
        return pts
    kx = math.cos(math.radians(40.7))

    def dist(p, a, b):
        ax, ay, bx, by, px, py = a[1] * kx, a[0], b[1] * kx, b[0], p[1] * kx, p[0]
        dx, dy = bx - ax, by - ay
        if dx == dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, None
        for k in range(i + 1, j):
            d = dist(pts[k], pts[i], pts[j])
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(pts, keep) if k]


def main(src: str, out: str) -> None:
    z = zipfile.ZipFile(src)
    routes = {}
    for r in rows(z, "routes.txt"):
        routes[r["route_id"]] = {"name": r["route_short_name"] or r["route_id"], "long": r["route_long_name"],
                                 "color": "#" + (r["route_color"] or "808183"),
                                 "text": "#" + (r["route_text_color"] or "FFFFFF")}

    stations, parent = {}, {}
    for s in rows(z, "stops.txt"):
        if s["location_type"] == "1":
            stations[s["stop_id"]] = [s["stop_name"], round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)]
        elif s["parent_station"]:
            parent[s["stop_id"]] = s["parent_station"]

    trip_route, trip_shape = {}, {}
    for t in rows(z, "trips.txt"):
        trip_route[t["trip_id"]] = t["route_id"]
        trip_shape[t["trip_id"]] = t["shape_id"]

    # previous stop + travel time, per (route, stop) with the stop's N/S direction suffix
    prev_counts: dict[tuple, Counter] = defaultdict(Counter)
    travel: dict[tuple, list[int]] = defaultdict(list)
    serves: dict[str, set] = defaultdict(set)
    last_trip, last_stop, last_time = None, None, None
    for st in rows(z, "stop_times.txt"):
        trip, stop = st["trip_id"], st["stop_id"]
        t = secs(st["arrival_time"] or st["departure_time"])
        route = trip_route.get(trip)
        if route is None:
            continue
        serves[parent.get(stop, stop)].add(route)
        if trip == last_trip and last_stop is not None:
            prev_counts[(route, stop)][last_stop] += 1
            travel[(route, last_stop, stop)].append(t - last_time)
        last_trip, last_stop, last_time = trip, stop, t

    prev = {}
    for (route, stop), c in prev_counts.items():
        p, _ = c.most_common(1)[0]
        tt = statistics.median(travel[(route, p, stop)])
        prev.setdefault(route, {})[stop] = [p, max(30, int(tt))]

    # one simplified polyline per distinct shape geometry, per route
    pts: dict[str, list] = defaultdict(list)
    for s in rows(z, "shapes.txt"):
        pts[s["shape_id"]].append((int(s["shape_pt_sequence"]), float(s["shape_pt_lat"]), float(s["shape_pt_lon"])))
    shape_route = {}
    for trip, shp in trip_shape.items():
        if shp:
            shape_route.setdefault(shp, trip_route[trip])
    lines = defaultdict(list)
    seen = set()
    for shp, plist in pts.items():
        route = shape_route.get(shp) or shp.split(".")[0]
        if route not in routes:
            continue
        plist.sort()
        poly = simplify([(lat, lon) for _, lat, lon in plist], tol=0.00012)
        key = (route, tuple((round(a, 3), round(b, 3)) for a, b in sorted(poly)))
        if key in seen:
            continue
        seen.add(key)
        lines[route].append([[round(a, 5), round(b, 5)] for a, b in poly])

    for sid in stations:
        stations[sid].append(sorted(serves.get(sid, ())))
    data = {"source": "MTA static GTFS (web.mta.info/developers)", "routes": routes, "stations": stations,
            "prev": prev, "lines": lines}
    with open(out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"{len(routes)} routes, {len(stations)} stations, {sum(len(v) for v in prev.values())} stop links, "
          f"{sum(len(v) for v in lines.values())} polylines -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
