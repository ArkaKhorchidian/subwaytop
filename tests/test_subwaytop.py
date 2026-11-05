import asyncio
import math
from pathlib import Path

import pytest

from subwaytop import model
from subwaytop.canvas import Canvas, View, fit, mix
from subwaytop.gtfsrt import parse

def plain(renderable) -> str:
    from rich.console import Console
    c = Console(width=60, record=True, file=open("/dev/null", "w") if Path("/dev/null").exists() else None)
    c.print(renderable)
    return c.export_text()


REC = Path(__file__).parent / "fixtures" / "rec"  # two real snapshots of the L and G feeds


@pytest.fixture(scope="module")
def snaps():
    return model.load_recording(REC)


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
def test_decoder_matches_official_bindings():
    pb2 = pytest.importorskip("google.transit.gtfs_realtime_pb2")
    for f in sorted(REC.glob("*/*.pb")):
        data = f.read_bytes()
        ref = pb2.FeedMessage()
        ref.ParseFromString(data)
        mine = parse(data)
        assert mine.timestamp == ref.header.timestamp
        n = 0
        for e in ref.entity:
            if e.HasField("trip_update"):
                tu = e.trip_update
                t = mine.trips[tu.trip.trip_id + tu.trip.start_date]
                assert t.route_id == tu.trip.route_id
                assert [(s.stop_id, s.arrival, s.departure) for s in t.stops] == \
                       [(s.stop_id, s.arrival.time or None, s.departure.time or None) for s in tu.stop_time_update]
                n += 1
            if e.HasField("vehicle"):
                v = e.vehicle
                t = mine.trips[v.trip.trip_id + v.trip.start_date]
                assert t.assigned and t.at_stop == v.stop_id
        assert n > 5


def test_decoder_handles_all_wire_types():
    # field 1 varint 150, field 2 fixed64, field 3 fixed32, field 4 bytes "hi"
    data = bytes([0x08, 0x96, 0x01, 0x11]) + (7).to_bytes(8, "little") + bytes([0x1D]) + (9).to_bytes(4, "little") \
        + bytes([0x22, 0x02]) + b"hi"
    from subwaytop.gtfsrt import fields
    assert list(fields(data)) == [(1, 0, 150), (2, 1, 7), (3, 5, 9), (4, 2, b"hi")]


# ---------------------------------------------------------------------------
# Network data
# ---------------------------------------------------------------------------
def test_static_network_is_complete():
    net = model.network()
    assert {"1", "4", "7", "A", "G", "L", "N", "Q", "SI"} <= set(net["routes"])
    assert len(net["stations"]) > 450
    for sid, (name, lat, lon, routes) in net["stations"].items():
        assert 40.4 < lat < 41.0 and -74.3 < lon < -73.6, sid
    # every L stop going north has a previous stop and a sane travel time
    for stop, (prev, secs) in net["prev"]["L"].items():
        assert prev.endswith(stop[-1]) and 30 <= secs <= 600


@pytest.mark.parametrize("query,name", [
    ("union sq", "14 St-Union Sq"), ("Times Square", "Times Sq-42 St"), ("bedford av", "Bedford Av"),
    ("atlantic", "Atlantic Av-Barclays Ctr"),
])
def test_find_station(query, name):
    assert model.find_station(query)[0]["name"] == name


def test_station_complexes_merge_platforms():
    ts = model.find_station("times sq")[0]
    assert len(ts["ids"]) >= 3 and {"1", "7", "N", "S"} <= {model.display_route(r) for r in ts["routes"]}


def test_display_route():
    assert [model.display_route(r) for r in ("6X", "7X", "FX", "GS", "FS", "H", "A")] == ["6", "7", "F", "S", "S", "S", "A"]


# ---------------------------------------------------------------------------
# Live state from real recorded feeds
# ---------------------------------------------------------------------------
def test_positions_are_on_the_line(snaps):
    s = snaps[0]
    pos = model.positions(s, s.taken)
    assert len(pos) > 15
    stations = model.network()["stations"]
    for p in pos:
        assert model.display_route(p.route) in ("L", "G")
        # a train is never more than ~2 km from a station its line serves
        nearest = min(model.distance_m(p.lat, p.lon, st[1], st[2]) for st in stations.values()
                      if p.route in st[3] or model.display_route(p.route) in st[3])
        assert nearest < 2000


def test_trains_move_smoothly(snaps):
    s = snaps[0]
    a = {p.trip_id: p for p in model.positions(s, s.taken)}
    b = {p.trip_id: p for p in model.positions(s, s.taken + 15)}
    moves = [model.distance_m(a[k].lat, a[k].lon, b[k].lat, b[k].lon) for k in a if k in b]
    assert moves and max(moves) < 600  # nobody teleports in 15 s
    assert any(m > 5 for m in moves)  # and some trains are actually moving


def test_scheduled_trips_are_not_drawn():
    from subwaytop.gtfsrt import StopTime, Trip
    far = Trip(trip_id="x", route_id="L", stops=[StopTime("L01N", arrival=10_000)], assigned=True)
    assert model.position(far, now=0) is None  # departs in 2.8 hours


def test_board(snaps):
    s = snaps[0]
    st = model.find_station("bedford av")[0]
    b = model.board(s, st, s.taken)
    assert b["N"] and b["S"]
    for d in "NS":
        mins = [x.minutes for x in b[d]]
        assert mins == sorted(mins) and all(m >= 0 for m in mins)
        assert all(x.route == "L" for x in b[d])
    assert {x.destination for x in b["N"]} == {"8 Av"} and {x.destination for x in b["S"]} == {"Canarsie-Rockaway Pkwy"}


def test_alerts(snaps):
    al = model.active_alerts(snaps[0].alerts, snaps[0].taken)
    assert al and all(a["header"] and a["routes"] for a in al)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_canvas_braille_and_projection():
    c = Canvas(4, 2)
    c.dot(0, 0, "#ff0000")
    c.dot(1, 3, "#ff0000")
    c.line(2, 0, 7, 7, "#00ff00")
    out = c.render().plain.split("\n")
    assert out[0][0] == chr(0x2800 + 0x01 + 0x80) and len(out) == 2 and all(len(r) == 4 for r in out)
    v = fit([(40.6, -74.0), (40.9, -73.8)], 40, 20)
    x, y = v.project(v.lat, v.lon, 40, 20)
    assert (x, y) == (40, 40)
    lat, lon = v.unproject(x, y, 40, 20)
    assert math.isclose(lat, v.lat) and math.isclose(lon, v.lon)
    assert mix("#ffffff", 0.0) == "#0d1117" and mix("#ffffff", 1.0) == "#ffffff"


def test_app_renders_and_responds(snaps):
    from subwaytop.app import SubwayApp

    async def run():
        app = SubwayApp(replay=snaps, speed=10)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            assert "trains running" in str(app.query_one("#head").render())
            await pilot.press("plus")
            await pilot.press("L")
            assert app.focus_line == "L"
            await pilot.press("slash")
            await pilot.press(*"bedford")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert app.station and app.station["name"] == "Bedford Av"
            assert "NORTHBOUND" in plain(app._board(app.now()))
            await pilot.press("escape")
            assert app.station is None and app.focus_line is None
            await pilot.press("q")
    asyncio.run(run())
