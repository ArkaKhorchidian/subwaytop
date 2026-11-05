"""Draw the subway map with braille characters: each terminal cell is a 2x4 grid of dots."""
from __future__ import annotations

import math
from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col]
BG = "#0d1117"


def mix(color: str, amount: float, bg: str = BG) -> str:
    """Blend `color` toward the background: amount=1 is the full color."""
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(b[i] + (c[i] - b[i]) * amount):02x}" for i in range(3))


@dataclass
class View:
    lat: float
    lon: float
    scale: float  # dots per degree of latitude

    def project(self, lat: float, lon: float, w: int, h: int) -> tuple[int, int]:
        k = math.cos(math.radians(self.lat))
        x = (lon - self.lon) * self.scale * k + w
        y = (self.lat - lat) * self.scale + 2 * h
        return round(x), round(y)

    def unproject(self, x: float, y: float, w: int, h: int) -> tuple[float, float]:
        k = math.cos(math.radians(self.lat))
        return self.lat - (y - 2 * h) / self.scale, self.lon + (x - w) / (self.scale * k)


def fit(points: list[tuple[float, float]], w: int, h: int, margin: float = 0.94) -> View:
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat0, lon0 = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    k = math.cos(math.radians(lat0))
    scale = margin * min((4 * h) / (max(lats) - min(lats)), (2 * w) / ((max(lons) - min(lons)) * k))
    return View(lat0, lon0, scale)


class Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.bits = [0] * (w * h)
        self.color: list[str | None] = [None] * (w * h)
        self.prio = [0] * (w * h)
        self.over: dict[int, tuple[str, str]] = {}  # cell -> (character, style), drawn on top

    def dot(self, x: int, y: int, color: str, prio: int = 0) -> None:
        if 0 <= x < 2 * self.w and 0 <= y < 4 * self.h:
            i = (y >> 2) * self.w + (x >> 1)
            self.bits[i] |= BITS[y & 3][x & 1]
            if prio >= self.prio[i]:
                self.prio[i], self.color[i] = prio, color

    def line(self, x0: int, y0: int, x1: int, y1: int, color: str, prio: int = 0) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        if max(dx, -dy) > 4 * (self.w + self.h) * 4:  # wildly off screen
            return
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.dot(x0, y0, color, prio)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def put(self, cx: int, cy: int, ch: str, style: str) -> bool:
        if 0 <= cx < self.w and 0 <= cy < self.h:
            self.over[cy * self.w + cx] = (ch, style)
            return True
        return False

    def free(self, cx: int, cy: int, n: int) -> bool:
        return all(0 <= cx + i < self.w and 0 <= cy < self.h and (cy * self.w + cx + i) not in self.over
                   for i in range(n))

    def text(self, cx: int, cy: int, s: str, style: str) -> None:
        for i, ch in enumerate(s):
            self.put(cx + i, cy, ch, style)

    def render(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        styles: dict[str, Style] = {}
        for row in range(self.h):
            run, run_style = [], None
            base = row * self.w
            for col in range(self.w):
                i = base + col
                if i in self.over:
                    ch, st = self.over[i]
                elif self.bits[i]:
                    ch, st = chr(0x2800 + self.bits[i]), self.color[i]
                else:
                    ch, st = " ", ""
                if st != run_style and run:
                    out.append("".join(run), styles.setdefault(run_style, Style.parse(run_style) if run_style else Style()))
                    run = []
                run_style = st
                run.append(ch)
            if run:
                out.append("".join(run), styles.setdefault(run_style, Style.parse(run_style) if run_style else Style()))
            if row < self.h - 1:
                out.append("\n")
        return out
