"""The printed parts in the assembly frame (see __init__). Print orientation lives in build.py.

- body: top, back and sides; printed upside down with the top on the bed (smooth, no supports)
- front: panel behind the window frame; printed face down, so any grille prints cleanly
- base: bottom plate with the board standoffs; screwed into the corner columns
- speaker_ring: clamps the speaker to the front panel
- ear (bear only), figure: base for a figure with a pocket for an NFC coin tag
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from manifold3d import CrossSection, Manifold

from myboxi_case import characters, patterns, text
from myboxi_case.components import BOARDS, PN532, board_footprint, board_holes
from myboxi_case.config import CaseConfig
from myboxi_case.geom import (
    bounds,
    box,
    chamfered_prism,
    circle,
    cylinder_y,
    cylinder_z,
    prism,
    profile,
    rect,
    rounded_rect,
    section_union,
    union,
    xz_slab,
)
from myboxi_case.layout import (
    BASE,
    CHAMFER,
    COLUMN_R,
    ENGRAVE,
    PANEL_T,
    RAIL_OVERLAP,
    RAIL_T,
    RING_R,
    RING_W,
    SOCKET_HOLE_DX,
    SPEAKER_RIM,
    STANDOFF_H,
    STANDOFF_R,
    SYMBOLS,
    TOP,
    WALL,
    WINDOW_R,
    Layout,
)

PILOT_M3 = {"self_tap": 1.25, "insert": 2.0}  # radius: self-tapping M3, or M3 heat-set insert
PILOT_M25 = {"self_tap": 1.1, "insert": 1.75}
EAR_T = 5.0
EAR_TENON = (16.0, 4.6, 6.0)  # width, thickness, depth below the top


@dataclass(frozen=True)
class Shape:
    solid: Manifold
    inlay: Manifold  # engraved areas, for a second colour (empty if none)


class Geometry:
    """Derived positions shared by the parts."""

    def __init__(self, cfg: CaseConfig, layout: Layout) -> None:
        self.cfg = cfg
        self.lay = layout
        self.tol = cfg.tolerance
        self.outline = profile(layout.width, layout.depth, layout.r_front, layout.r_back)
        self.inner = self.outline.offset(-WALL)
        self.panel_back = WALL + PANEL_T
        self.rail_y0 = self.panel_back + self.tol
        self.columns = self._columns()

    def _columns(self) -> list[tuple[float, float]]:
        lay = self.lay
        front_y = self.rail_y0 + RAIL_T + 1.0 + COLUMN_R
        fx = WALL + COLUMN_R - 0.4
        ri = lay.r_back - WALL
        k = (ri - COLUMN_R + 0.4) / math.sqrt(2)
        back_y = lay.depth - lay.r_back + k
        return [
            (fx, front_y),
            (lay.width - fx, front_y),
            (lay.r_back - k, back_y),
            (lay.width - lay.r_back + k, back_y),
        ]

    @property
    def panel_z(self) -> tuple[float, float]:
        return (BASE + self.tol, self.lay.height - TOP - self.tol)

    @property
    def outer_solid(self) -> Manifold:
        return chamfered_prism(self.outline, self.lay.height, CHAMFER)

    def pilot(self, table: dict[str, float]) -> float:
        return table[self.cfg.fastening]


def _engrave_top(g: Geometry, section: CrossSection) -> Manifold:
    return prism(section, g.lay.height - ENGRAVE, g.lay.height + 0.01)


def _engrave_front(section: CrossSection) -> Manifold:
    """Engraving into the visible face of the front panel (y = WALL)."""
    return xz_slab(section, WALL - 0.01, WALL + ENGRAVE)


def body(g: Geometry) -> Shape:
    lay, tol = g.lay, g.tol
    top = lay.height - TOP
    outer = g.outer_solid
    shell = outer - prism(g.inner, -1.0, top)

    # U-shaped window, open at the bottom: nothing to bridge when printed upside down.
    wx0, wx1 = lay.window_x
    window = rounded_rect(wx0, -20.0, wx1, lay.window_top, WINDOW_R)
    shell -= xz_slab(window, -1.0, WALL + 0.01)

    px0, px1 = lay.panel_x
    rails = [
        box(0.0, g.rail_y0, BASE, px0 + RAIL_OVERLAP, g.rail_y0 + RAIL_T, top + 0.01),
        box(px1 - RAIL_OVERLAP, g.rail_y0, BASE, lay.width, g.rail_y0 + RAIL_T, top + 0.01),
        box(px0, g.rail_y0, top - 6.0, px1, g.rail_y0 + RAIL_T, top + 0.01),
    ]
    pilot = g.pilot(PILOT_M3)
    columns = [cylinder_z(COLUMN_R, x, y, BASE, top + 0.01) for x, y in g.columns]
    features = union([*rails, *columns, _pn532_frame(g, tol)]) ^ outer
    shell = union([shell, features])
    shell -= union([cylinder_z(pilot, x, y, BASE - 1.0, BASE + 14.0) for x, y in g.columns])

    # Buttons, ear slots and the socket go through the walls.
    holes = [
        cylinder_z(g.cfg.button / 2 + tol, b.x, b.y, top - 1.0, lay.height + 1.0)
        for b in lay.buttons
    ]
    if lay.character is not None:
        tw = characters.topper(lay.character).tenon
        tt = EAR_TENON[1]
        for ex, ey in ear_positions(lay):
            ty0 = ey - EAR_T / 2  # the tenon is flush with the topper's front face
            holes.append(
                box(
                    ex - tw / 2 - tol,
                    ty0 - tol,
                    top - 1.0,
                    ex + tw / 2 + tol,
                    ty0 + tt + tol,
                    lay.height + 1.0,
                )
            )
    if lay.character == "unicorn":
        hx, hy = horn_position(lay)
        holes.append(cylinder_z(1.7, hx, hy, top - 1.0, lay.height + 1.0))  # M3 from inside
    sx, sz = lay.socket
    socket_hole = rounded_rect(sx - 6.5, sz - 3.5, sx + 6.5, sz + 3.5, 3.0)
    screw_holes = [circle(1.6, sx + dx, sz) for dx in (-SOCKET_HOLE_DX, SOCKET_HOLE_DX)]
    holes.append(
        xz_slab(section_union([socket_hole, *screw_holes]), lay.depth - WALL - 1.0, lay.depth + 1.0)
    )
    if g.cfg.board == "pi4":
        vents = section_union(
            [rounded_rect(sx + 20 + 6 * i, 20.0, sx + 23 + 6 * i, 40.0, 1.5) for i in range(5)]
        )
        holes.append(xz_slab(vents, lay.depth - WALL - 1.0, lay.depth + 1.0))
    shell -= union(holes)

    # Engravings on the top face (on the print bed): figure spot and button symbols.
    fx, fy = lay.figure
    marks = [circle(RING_R + RING_W / 2, fx, fy) - circle(RING_R - RING_W / 2, fx, fy)]
    for b in lay.buttons:
        marks.append(patterns.symbol(SYMBOLS[b.action]).translate((b.x, b.y + b.symbol_dy)))
    inlay = _engrave_top(g, section_union(marks)) ^ outer
    return Shape(shell - inlay, inlay)


def _pn532_frame(g: Geometry, tol: float) -> Manifold:
    """Corner posts that hold the NFC board right under the top plate, with small snap lips.

    The lips are 45° wedges: the board slides over them and they print upside down.
    """
    lay = g.lay
    fx, fy = lay.figure
    w, d = PN532
    top = lay.height - TOP
    x0, x1 = fx - w / 2 - tol, fx + w / 2 + tol
    y0, y1 = fy - d / 2 - tol, fy + d / 2 + tol
    board_bottom = top - 1.6 - tol
    z0 = board_bottom - 1.2
    leg, t, lip = 7.0, 1.6, 1.2
    posts: list[Manifold] = []
    for cx, sx in ((x0, -1), (x1, 1)):
        for cy, sy in ((y0, -1), (y1, 1)):
            # L-shaped post outside the board corner; sx/sy point away from the board.
            xa, xb = sorted((cx - sx * leg, cx + sx * t))
            ya, yb = sorted((cy - sy * leg, cy + sy * t))
            xo = sorted((cx, cx + sx * t))
            yo = sorted((cy, cy + sy * t))
            posts.append(box(xo[0], ya, z0, xo[1], yb, top + 0.01))
            posts.append(box(xa, yo[0], z0, xb, yo[1], top + 0.01))
            inner_x = sorted((cx, cx - sx * lip))
            inner_y = sorted((cy, cy - sy * lip))
            flat = box(
                inner_x[0], inner_y[0], board_bottom - 0.01, inner_x[1], inner_y[1], board_bottom
            )
            edge = box(
                cx - 0.01,
                cy - 0.01,
                board_bottom - lip,
                cx + 0.01,
                cy + 0.01,
                board_bottom - lip + 0.01,
            )
            posts.append(Manifold.batch_hull([flat, edge]))
    return union(posts)


def front(g: Geometry) -> Shape:
    lay, cfg = g.lay, g.cfg
    px0, px1 = lay.panel_x
    z0, z1 = g.panel_z
    tol = g.tol
    panel = xz_slab(rect(px0 + tol, z0, px1 - tol, z1), WALL, g.panel_back)
    sx, sz = lay.speaker
    r = cfg.speaker / 2
    panel -= xz_slab(
        patterns.grille(cfg.grille, r - 2.0).translate((sx, sz)), WALL - 1.0, g.panel_back + 1.0
    )

    # Speaker seat on the back: centring ring and three bosses for the clamp ring.
    back = g.panel_back
    seat = circle(r + 0.3 + 1.6, sx, sz) - circle(r + 0.3, sx, sz)
    parts = [panel, xz_slab(seat, back - 0.01, back + 2.0)]
    pilot = g.pilot(PILOT_M25)
    boss_holes: list[Manifold] = []
    for x, z in speaker_bosses(lay, cfg.speaker):
        parts.append(cylinder_y(3.0, x, z, back - 0.01, back + SPEAKER_RIM - 0.3))
        boss_holes.append(cylinder_y(pilot, x, z, back - 1.0, back + SPEAKER_RIM))
    solid = union(parts) - union(boss_holes)

    marks: list[CrossSection] = []
    nx0, nz0, nx1, nz1 = lay.name_box
    if cfg.name:
        label = text.fitted(cfg.name, nx1 - nx0, min(nz1 - nz0, 16.0) * 0.72, 5.0)
        _, by0, _, by1 = bounds(label)
        marks.append(label.translate(((nx0 + nx1) / 2, (nz0 + nz1) / 2 - (by0 + by1) / 2)))
    if lay.character is not None:
        face = characters.face_marks(lay.character, r).translate((sx, sz))
        # Keep a bar between the face and the grille openings.
        marks.append(face - patterns.grille(cfg.grille, r - 2.0).translate((sx, sz)).offset(1.0))
    if not marks:
        return Shape(solid, Manifold())
    inlay = _engrave_front(section_union(marks))
    return Shape(solid - inlay, inlay)


def speaker_bosses(lay: Layout, diameter: float) -> list[tuple[float, float]]:
    sx, sz = lay.speaker
    rr = diameter / 2 + 4.0
    return [
        (sx + rr * math.cos(math.radians(a)), sz + rr * math.sin(math.radians(a)))
        for a in (90, 210, 330)
    ]


def speaker_ring(g: Geometry) -> Shape:
    lay, d = g.lay, g.cfg.speaker
    sx, sz = lay.speaker
    y0 = g.panel_back + SPEAKER_RIM
    ring = circle(d / 2 + 4.0, sx, sz) - circle(d / 2 - 3.5, sx, sz)
    lugs = [circle(3.0, x, z) for x, z in speaker_bosses(lay, d)]
    holes = [circle(1.45, x, z) for x, z in speaker_bosses(lay, d)]
    shape = section_union([ring, *lugs]) - section_union(holes)
    return Shape(xz_slab(shape, y0, y0 + 2.4), Manifold())


def base(g: Geometry) -> Shape:
    lay, cfg, tol = g.lay, g.cfg, g.tol
    wx0, wx1 = lay.window_x
    footprint = section_union([g.inner.offset(-tol), rect(wx0 + tol, 0.0, wx1 - tol, WALL + 1.0)])
    plate = prism(footprint, 0.0, BASE)
    parts = [plate]
    cuts: list[Manifold] = []
    for x, y in g.columns:
        cuts.append(cylinder_z(1.7, x, y, -1.0, BASE + 1.0))
        cuts.append(Manifold.cylinder(1.6, 3.3, 1.7).translate((x, y, -0.01)))  # countersink, 45°
    spec = BOARDS[cfg.board]
    pilot = g.pilot(PILOT_M25)
    for hx, hy in board_holes(lay.board, spec):
        parts.append(cylinder_z(STANDOFF_R, hx, hy, BASE - 0.01, BASE + STANDOFF_H))
        cuts.append(cylinder_z(pilot, hx, hy, BASE - 1.0, BASE + STANDOFF_H + 1.0))
    ax, ay = lay.amp
    parts.append(box(ax - 12.0, ay - 3.0, BASE - 0.01, ax + 12.0, ay + 3.0, BASE + 4.0))
    cuts.append(
        box(ax - 10.5, ay - 0.9 - tol / 2, BASE + 1.0, ax + 10.5, ay + 0.9 + tol / 2, BASE + 4.1)
    )
    if lay.powerbank is not None:
        px, py = lay.powerbank
        w, d = 93.0, 61.0
        for cx, cy, sx, sy in (
            (px, py, 1, 1),
            (px + w, py, -1, 1),
            (px, py + d, 1, -1),
            (px + w, py + d, -1, -1),
        ):
            ox = cx - 2.0 if sx > 0 else cx
            oy = cy - 2.0 if sy > 0 else cy
            parts.append(
                box(
                    ox,
                    min(cy, cy + sy * 12),
                    BASE - 0.01,
                    ox + 2.0,
                    max(cy, cy + sy * 12),
                    BASE + 12.0,
                )
            )
            parts.append(
                box(
                    min(cx, cx + sx * 12),
                    oy,
                    BASE - 0.01,
                    max(cx, cx + sx * 12),
                    oy + 2.0,
                    BASE + 12.0,
                )
            )
    if cfg.board == "pi4":
        bx0, by0, bx1, by1 = board_footprint(lay.board, spec)
        for i in range(6):
            x = bx0 + 12.0 + i * (bx1 - bx0 - 24.0) / 5
            cuts.append(box(x - 1.5, by0 + 12.0, -1.0, x + 1.5, by1 - 12.0, BASE + 1.0))
    return Shape(union(parts) - union(cuts), Manifold())


def ear_positions(lay: Layout) -> list[tuple[float, float]]:
    if lay.character is None:
        return []
    offset = characters.topper(lay.character).offset
    return [(offset, 11.0), (lay.width - offset, 11.0)]


def ear(g: Geometry, x: float, y: float) -> Shape:
    """Ear (or eye) standing on the top, parallel to the front; its tenon is glued into a slot.

    Printed lying on its front face, so the tenon is flush with that face (nothing floats).
    """
    if g.lay.character is None:
        raise ValueError("ears only belong to character forms")
    top = characters.topper(g.lay.character)
    h = g.lay.height
    _, tt, td = EAR_TENON
    outline = top.outline.translate((x, h))
    tenon = rect(x - top.tenon / 2, h - TOP - td + 2.0, x + top.tenon / 2, h + 0.5)
    y0 = y - EAR_T / 2
    solid = union([xz_slab(outline, y0, y0 + EAR_T), xz_slab(tenon, y0, y0 + tt)])
    inlay = xz_slab(top.inlay.translate((x, h)), y0 - 0.01, y0 + ENGRAVE)
    return Shape(solid - inlay, inlay)


HORN_R = 9.0
HORN_H = 40.0


def horn_position(lay: Layout) -> tuple[float, float]:
    return (lay.width / 2, 13.0)


def horn(g: Geometry) -> Shape:
    """Twisted unicorn horn; stands on its flat base and is screwed on from inside (M3)."""
    hx, hy = horn_position(g.lay)
    h = g.lay.height
    star = patterns.star(HORN_R - 1.2, HORN_R - 3.2).offset(1.2)
    spiral = star.extrude(HORN_H, n_divisions=40, twist_degrees=-300.0, scale_top=(0.08, 0.08))
    solid = spiral.translate((hx, hy, h)) - cylinder_z(g.pilot(PILOT_M3), hx, hy, h - 1.0, h + 9.0)
    return Shape(solid, Manifold())


def figure_base(g: Geometry) -> Shape:
    """Ø40 base for a figure, standing on the ring; closed pocket for a 25 mm NTAG213 coin tag.

    The pocket is closed: pause the print at 2.2 mm and drop the tag in.
    """
    fx, fy = g.lay.figure
    h = g.lay.height
    disc = Manifold.batch_hull(
        [
            cylinder_z(19.4, fx, fy, h, h + 0.6),
            cylinder_z(20.0, fx, fy, h + 0.6, h + 5.0),
            cylinder_z(19.0, fx, fy, h + 5.0, h + 6.0),
        ]
    )
    pocket = cylinder_z(12.8, fx, fy, h + 1.0, h + 2.2)
    return Shape(disc - pocket, Manifold())
