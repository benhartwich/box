"""Character forms on the cube: ears (or eyes, horn) on top and a face on the front panel.

Shapes are flat cross sections in millimetres. Toppers are drawn in x/z with x centred and z=0 on
the top surface; face marks are relative to the speaker centre on the front panel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from manifold3d import CrossSection

from myboxi_case.geom import circle, rect, regular, rounded_rect, section_union

Character = Literal["bear", "cat", "bunny", "unicorn", "frog"]
CHARACTERS: tuple[Character, ...] = ("bear", "cat", "bunny", "unicorn", "frog")


@dataclass(frozen=True)
class Topper:
    label: str
    outline: CrossSection  # above the top surface (z >= 0)
    inlay: CrossSection  # second colour on the front face
    offset: float  # distance of each topper's centre from the side walls
    tenon: float  # width of the tenon that goes into the slot


def eye(r: float = 4.5) -> CrossSection:
    return CrossSection.circle(r)


def nose(width: float = 11.0) -> CrossSection:
    """Rounded triangle, point down."""
    r = 2.2 if width >= 10 else 1.6
    pts = regular(3, width / 2 - r, rotation=-90)
    return CrossSection.batch_hull([circle(r, x, y) for x, y in pts])


def _capsule(x0: float, z0: float, x1: float, z1: float, w: float) -> CrossSection:
    return CrossSection.batch_hull([circle(w / 2, x0, z0), circle(w / 2, x1, z1)])


def _above(shape: CrossSection) -> CrossSection:
    return shape - rect(-40, -40, 40, 0)


def topper(character: Character) -> Topper:
    match character:
        case "bear":
            return Topper("Ohr", _above(circle(15.0, 0, 13.0)), circle(8.0, 0, 14.0), 24.0, 16.0)
        case "cat":
            ear = CrossSection.batch_hull([circle(2.5, 0, 24.0), rect(-13.0, 0.0, 13.0, 1.0)])
            inner = CrossSection.batch_hull([circle(1.2, 0, 17.5), rect(-7.0, 3.0, 7.0, 4.0)])
            return Topper("Ohr", ear, inner, 22.0, 16.0)
        case "bunny":
            ear = CrossSection.batch_hull([rect(-7.0, 0.0, 7.0, 1.0), circle(8.5, 0, 40.0)])
            inner = CrossSection.batch_hull([rect(-3.0, 6.0, 3.0, 7.0), circle(5.0, 0, 39.0)])
            return Topper("Ohr", ear, inner, 28.0, 10.0)
        case "unicorn":
            ear = CrossSection.batch_hull([circle(2.0, 0, 19.0), rect(-9.0, 0.0, 9.0, 1.0)])
            inner = CrossSection.batch_hull([circle(1.0, 0, 14.0), rect(-4.5, 3.0, 4.5, 4.0)])
            return Topper("Ohr", ear, inner, 22.0, 12.0)
        case "frog":
            return Topper("Auge", _above(circle(13.0, 0, 12.0)), circle(5.5, 0, 13.0), 26.0, 16.0)


def face(character: Character, r: float) -> list[CrossSection]:
    """Marks around a speaker grille of radius ``r`` (the speaker radius) at the origin."""
    match character:
        case "bear":
            return [
                eye().translate((-16.0, 29.0)),
                eye().translate((16.0, 29.0)),
                nose().translate((0, 21.5)),
                circle(r + 2.2) - circle(r + 1.0),
            ]
        case "cat":
            marks = [
                _capsule(-16.0, 26.0, -16.0, 31.0, 6.0),
                _capsule(16.0, 26.0, 16.0, 31.0, 6.0),
                nose(8.0).translate((0, 23.0)),
            ]
            for side in (-1, 1):
                for k in (-1, 0, 1):
                    marks.append(
                        _capsule(side * 24.0, 6.0 + 5.0 * k, side * 37.0, 4.0 + 8.0 * k, 1.4)
                    )
            return marks
        case "bunny":
            return [
                eye(4.0).translate((-15.0, 28.0)),
                eye(4.0).translate((15.0, 28.0)),
                nose(7.0).translate((0, 22.5)),
                rounded_rect(-3.8, -27.0, -0.5, -22.5, 0.8),
                rounded_rect(0.5, -27.0, 3.8, -22.5, 0.8),
            ]
        case "unicorn":
            marks: list[CrossSection] = []
            for side in (-1, 1):
                cx, cz = side * 16.0, 30.0
                lid = (circle(5.5, cx, cz) - circle(4.2, cx, cz)) - rect(cx - 7, cz, cx + 7, cz + 7)
                marks.append(lid)
                for angle in (-60.0, -90.0, -120.0):
                    a = math.radians(angle)
                    x0, z0 = cx + 5.0 * math.cos(a), cz + 5.0 * math.sin(a)
                    x1, z1 = cx + 8.0 * math.cos(a), cz + 8.0 * math.sin(a)
                    marks.append(_capsule(x0, z0, x1, z1, 1.3))
                marks.append(circle(3.5, side * 25.0, 18.0))
            return marks
        case "frog":
            smile = circle(27.4, 0, 6.0) - circle(26.0, 0, 6.0)
            keep = CrossSection.batch_hull(
                [circle(0.1, 0, 6.0), circle(0.1, -60.0, -36.0), circle(0.1, 60.0, -36.0)]
            )
            return [
                circle(1.6, -5.0, 24.0),
                circle(1.6, 5.0, 24.0),
                smile ^ keep,
            ]


def face_marks(character: Character, r: float) -> CrossSection:
    return section_union(face(character, r))
