"""Text outlines from the bundled Figtree ExtraBold (OFL-1.1) as manifold cross sections."""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from io import BytesIO
from typing import Any, cast

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from manifold3d import CrossSection, FillRule

from myboxi_case import geom
from myboxi_case.geom import bounds

FONT_FILES = ("figtree-latin-800-normal.woff", "figtree-latin-ext-800-normal.woff")
CURVE_STEPS = 6

Point = tuple[float, float]


class _PolygonPen(BasePen):
    """Flattens glyph outlines (quadratic and cubic curves) into closed polygons."""

    def __init__(self, glyph_set: Any) -> None:
        super().__init__(glyph_set)  # pyright: ignore[reportUnknownMemberType]
        self.contours: list[list[Point]] = []
        self._current: list[Point] = []

    def _moveTo(self, pt: Point) -> None:
        self._current = [pt]

    def _lineTo(self, pt: Point) -> None:
        self._current.append(pt)

    def _curveToOne(self, pt1: Point, pt2: Point, pt3: Point) -> None:
        x0, y0 = self._current[-1]
        for i in range(1, CURVE_STEPS + 1):
            t = i / CURVE_STEPS
            u = 1 - t
            self._current.append(
                (
                    u**3 * x0 + 3 * u * u * t * pt1[0] + 3 * u * t * t * pt2[0] + t**3 * pt3[0],
                    u**3 * y0 + 3 * u * u * t * pt1[1] + 3 * u * t * t * pt2[1] + t**3 * pt3[1],
                )
            )

    def _qCurveToOne(self, pt1: Point, pt2: Point) -> None:
        x0, y0 = self._current[-1]
        for i in range(1, CURVE_STEPS + 1):
            t = i / CURVE_STEPS
            u = 1 - t
            self._current.append(
                (
                    u * u * x0 + 2 * u * t * pt1[0] + t * t * pt2[0],
                    u * u * y0 + 2 * u * t * pt1[1] + t * t * pt2[1],
                )
            )

    def _closePath(self) -> None:
        if len(self._current) > 2:
            self.contours.append(self._current)
        self._current = []

    def _endPath(self) -> None:
        self._closePath()


class _Font:
    def __init__(self) -> None:
        self.glyphs: dict[str, tuple[Any, Any]] = {}  # char -> (glyph set, glyph name)
        self.upm = 1000
        self.cap_height = 700.0
        for name in FONT_FILES:
            data = files("myboxi_case").joinpath("fonts", name).read_bytes()
            font = TTFont(BytesIO(data))
            glyph_set = font.getGlyphSet()
            head: Any = font["head"]
            self.upm = int(head.unitsPerEm)
            os2 = font["OS/2"]
            self.cap_height = float(getattr(os2, "sCapHeight", 0) or 0.7 * self.upm)
            cmap = cast(dict[int, str], font.getBestCmap())  # pyright: ignore[reportUnknownMemberType]
            for code, glyph in cmap.items():
                self.glyphs.setdefault(chr(code), (glyph_set, glyph))


@cache
def _font() -> _Font:
    return _Font()


def supported(text: str) -> set[str]:
    """Characters of ``text`` the font cannot draw (empty set: all fine)."""
    glyphs = _font().glyphs
    return {ch for ch in text if ch != " " and ch not in glyphs}


def outline(text: str, cap_height: float) -> CrossSection:
    """``text`` as a cross section; baseline at y=0, horizontally centred on x=0."""
    font = _font()
    scale = cap_height / font.cap_height
    contours: list[list[Point]] = []
    x = 0.0
    for ch in text:
        glyph_set, glyph = font.glyphs.get(ch, font.glyphs[" "])
        pen = _PolygonPen(glyph_set)
        glyph_set[glyph].draw(pen)
        for contour in pen.contours:
            contours.append([((px + x) * scale, py * scale) for px, py in contour])
        x += float(glyph_set[glyph].width)
    if not contours:
        return CrossSection()
    section = geom.polygons(contours, FillRule.NonZero)
    x0, _, x1, _ = bounds(section)
    return section.translate((-(x0 + x1) / 2, 0))


def fitted(
    text: str, max_width: float, max_cap_height: float, min_cap_height: float
) -> CrossSection:
    """The largest text up to ``max_cap_height`` that fits ``max_width``; never below the minimum.

    Callers validate the length beforehand, so the minimum only guards against odd glyph widths.
    """
    probe = outline(text, max_cap_height)
    x0, _, x1, _ = bounds(probe)
    width = x1 - x0
    if width <= max_width or width == 0:
        return probe
    return outline(text, max(min_cap_height, max_cap_height * max_width / width))
