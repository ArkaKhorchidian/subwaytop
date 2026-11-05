"""A tiny GTFS-realtime decoder: just the fields subwaytop uses, no protobuf dependency.

Field numbers follow https://gtfs.org/realtime/proto/ . Unknown fields (including the MTA's
NYCT extensions) are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        byte = b[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, i
        shift += 7


def fields(b: bytes):
    """Yield (field_number, wire_type, value) where value is an int or a bytes slice."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 1:
            v = int.from_bytes(b[i:i + 8], "little")
            i += 8
        elif wt == 5:
            v = int.from_bytes(b[i:i + 4], "little")
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield num, wt, v


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


@dataclass
class StopTime:
    stop_id: str
    arrival: int | None = None
    departure: int | None = None

    @property
    def time(self) -> int | None:
        return self.arrival if self.arrival else self.departure


@dataclass
class Trip:
    trip_id: str = ""
    route_id: str = ""
    start_date: str = ""
    stops: list[StopTime] = field(default_factory=list)
    # from the VehiclePosition entity, when there is one
    status: int | None = None  # 0 incoming at, 1 stopped at, 2 in transit to
    at_stop: str = ""
    assigned: bool = False


@dataclass
class Feed:
    timestamp: int
    trips: dict[str, Trip]


def _descriptor(b: bytes, t: Trip) -> None:
    for num, _, v in fields(b):
        if num == 1:
            t.trip_id = v.decode()
        elif num == 5:
            t.route_id = v.decode()
        elif num == 3:
            t.start_date = v.decode()


def _event_time(b: bytes) -> int | None:
    for num, _, v in fields(b):
        if num == 2:
            return _signed64(v)
    return None


def _stop_time(b: bytes) -> StopTime:
    st = StopTime("")
    for num, _, v in fields(b):
        if num == 4:
            st.stop_id = v.decode()
        elif num == 2:
            st.arrival = _event_time(v)
        elif num == 3:
            st.departure = _event_time(v)
    return st


def parse(data: bytes) -> Feed:
    ts = 0
    trips: dict[str, Trip] = {}
    for num, _, v in fields(data):
        if num == 1:  # FeedHeader
            for hn, _, hv in fields(v):
                if hn == 3:
                    ts = hv
        elif num == 2:  # FeedEntity
            for en, _, ev in fields(v):
                if en == 3:  # TripUpdate
                    t = Trip()
                    for tn, _, tv in fields(ev):
                        if tn == 1:
                            _descriptor(tv, t)
                        elif tn == 2:
                            t.stops.append(_stop_time(tv))
                    key = t.trip_id + t.start_date
                    old = trips.get(key)
                    if old:  # vehicle came first
                        old.stops, old.route_id = t.stops, old.route_id or t.route_id
                    else:
                        trips[key] = t
                elif en == 4:  # VehiclePosition
                    t = Trip(assigned=True)
                    for vn, _, vv in fields(ev):
                        if vn == 1:
                            _descriptor(vv, t)
                        elif vn == 4:
                            t.status = vv
                        elif vn == 7:
                            t.at_stop = vv.decode()
                    key = t.trip_id + t.start_date
                    old = trips.get(key)
                    if old:
                        old.status, old.at_stop, old.assigned = t.status, t.at_stop, True
                    else:
                        trips[key] = t
    return Feed(ts, trips)
