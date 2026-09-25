"""Flat shapes: button symbols, speaker grilles, bear face (cross sections in mm, centred)."""

from __future__ import annotations

import math

from manifold3d import CrossSection

from myboxi_case.config import Grille
from myboxi_case.geom import bounds, circle, polygon, rect, regular, rounded_rect, section_union
from myboxi_case.layout import Symbol

MAX_HOLE = 5.0  # keeps fingers and pens out (documented in docs/gehaeuse.md)
MIN_BAR = 0.8  # thinnest material between openings


def symbol(kind: Symbol, size: float = 8.0) -> CrossSection:
    s = size
    bar = s * 0.22
    match kind:
        case "plus":
            return section_union(
                [rect(-s / 2, -bar / 2, s / 2, bar / 2), rect(-bar / 2, -s / 2, bar / 2, s / 2)]
            )
        case "minus":
            return rect(-s / 2, -bar / 2, s / 2, bar / 2)
        case "play_pause":
            tri = polygon([(-s / 2, -s / 2), (s * 0.05, 0), (-s / 2, s / 2)])
            bars = [
                rect(s * 0.18, -s / 2, s * 0.18 + bar, s / 2),
                rect(s / 2 - bar, -s / 2, s / 2, s / 2),
            ]
            return section_union([tri, *bars])
        case "next":
            tri = polygon([(-s / 2, -s / 2), (s * 0.25, 0), (-s / 2, s / 2)])
            return section_union([tri, rect(s / 2 - bar, -s / 2, s / 2, s / 2)])


def _star(r_out: float, r_in: float) -> CrossSection:
    points: list[tuple[float, float]] = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(90 + 36 * i)
        points.append((r * math.cos(a), r * math.sin(a)))
    return polygon(points)


def _heart(width: float) -> CrossSection:
    r = width / 4
    tip = circle(0.3, 0, -width * 0.62)
    left = CrossSection.batch_hull([circle(r, -r, 0), tip])
    right = CrossSection.batch_hull([circle(r, r, 0), tip])
    shape = section_union([left, right])
    x0, y0, x1, y1 = bounds(shape)
    return shape.translate((-(x0 + x1) / 2, -(y0 + y1) / 2))


def _hex_grid(radius: float, pitch: float, item: CrossSection, item_r: float) -> CrossSection:
    """``item`` repeated on a hexagonal grid; only copies fully inside the circle."""
    items: list[CrossSection] = []
    row_h = pitch * math.sqrt(3) / 2
    rows = int(radius / row_h) + 1
    for j in range(-rows, rows + 1):
        y = j * row_h
        offset = pitch / 2 if j % 2 else 0.0
        cols = int(radius / pitch) + 2
        for i in range(-cols, cols + 1):
            x = i * pitch + offset
            if math.hypot(x, y) + item_r <= radius:
                items.append(item.translate((x, y)))
    return section_union(items)


def grille(kind: Grille, radius: float) -> CrossSection:
    """Openings in front of the speaker, inside a circle of ``radius``."""
    match kind:
        case "dots":
            return _hex_grid(radius, 5.4, CrossSection.circle(1.75), 1.75)
        case "stars":
            return _hex_grid(radius, 7.3, _star(3.4, 2.0), 3.4)
        case "hearts":
            return _hex_grid(radius, 7.6, _heart(6.8), 3.7)
        case "lines":
            slots: list[CrossSection] = []
            pitch = 5.5
            n = int(radius / pitch)
            for j in range(-n, n + 1):
                y = j * pitch
                half = math.sqrt(max(radius * radius - (abs(y) + 1.5) ** 2, 0)) - 0.5
                if half > 4:
                    slots.append(rounded_rect(-half, y - 1.5, half, y + 1.5, 1.5))
            return section_union(slots)


def eye(r: float = 4.5) -> CrossSection:
    return CrossSection.circle(r)


def nose(width: float = 11.0) -> CrossSection:
    """Rounded triangle, point down."""
    r = 2.2
    pts = regular(3, width / 2 - r, rotation=-90)
    return CrossSection.batch_hull([circle(r, x, y) for x, y in pts])
