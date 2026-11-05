"""subwaytop command line."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, model


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="subwaytop", description="Every NYC subway train, live, in your terminal.",
                                epilog="keys: / station · + - zoom · arrows pan · A-Z 1-7 focus a line · esc reset · q quit")
    p.add_argument("station", nargs="?", help="open a station's departures, e.g. 'union sq'")
    p.add_argument("--line", help="focus one line, e.g. L or 7")
    p.add_argument("--replay", type=Path, metavar="DIR", help="play back a recording made with --record")
    p.add_argument("--speed", type=float, default=20.0, help="replay speed (default 20x)")
    p.add_argument("--record", type=Path, metavar="DIR", help="save live snapshots every 30 s instead of showing the map")
    p.add_argument("--minutes", type=float, default=30.0, help="how long to --record (default 30)")
    p.add_argument("--zoom", type=float, default=1.0, help="start zoomed in, e.g. 2.5 for Manhattan")
    p.add_argument("--board", action="store_true", help="print the station's departures once and exit")
    p.add_argument("--exit-after", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"subwaytop {__version__}")
    a = p.parse_args(argv)

    if a.station and not model.find_station(a.station):
        print(f"No station matches “{a.station}”.", file=sys.stderr)
        return 1
    line = model.display_route(a.line.upper()) if a.line else None

    if a.record:
        end = time.time() + a.minutes * 60
        n = 0
        while time.time() < end:
            t0 = time.time()
            raw, al = model.fetch_all()
            model.save_snapshot(a.record, raw, al, t0)
            n += 1
            print(f"\rsaved {n} snapshots to {a.record}", end="", flush=True)
            time.sleep(max(1.0, 30 - (time.time() - t0)))
        print()
        return 0

    if a.board:
        st = model.find_station(a.station or "")[:1]
        if not st:
            print("--board needs a station name", file=sys.stderr)
            return 1
        snap = model.build_snapshot(*model.fetch_all())
        b = model.board(snap, st[0], time.time())
        print(st[0]["name"])
        for d, label in (("N", "Northbound"), ("S", "Southbound")):
            print(f"  {label}")
            for dep in b[d][:6]:
                print(f"    {model.display_route(dep.route):>3}  {dep.destination:<30} {dep.minutes:4.0f} min")
        return 0

    from .app import SubwayApp

    if a.replay:
        snaps = model.load_recording(a.replay)
        if not snaps:
            print(f"No snapshots in {a.replay}", file=sys.stderr)
            return 1
        app = SubwayApp(replay=snaps, speed=a.speed, focus=line, station=a.station, exit_after=a.exit_after, zoom=a.zoom)
    else:
        app = SubwayApp(source=lambda: model.build_snapshot(*model.fetch_all()), focus=line, station=a.station,
                        exit_after=a.exit_after, zoom=a.zoom)
    app.run()
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
