"""Automatic checks of a built case: printable, parts fit, room for every bought part.

These cannot replace a test print, but they catch the mistakes that a layout change or a new
option would otherwise only reveal on the printer (docs/gehaeuse.md).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from manifold3d import CrossSection, Manifold, OpType

from myboxi_case import patterns
from myboxi_case.build import CaseModel, Piece
from myboxi_case.components import PCB, PN532
from myboxi_case.geom import bbox, bounds, box, mesh_arrays, polygons
from myboxi_case.layout import TOP

MAX_PRINT = 180.0  # every part fits small printers too (A1 mini, Prusa MINI)
MIN_GAP = 0.5  # between bought parts and printed parts they are not fastened to
OVERHANG_COS = -0.72  # steeper than about 44° from vertical needs support
MAX_BRIDGE = 16.0
MIN_FEATURE = 0.8
NFC_MARGIN = 5.0
NFC_DEPTH = 15.0
MIN_OPEN_AREA = 0.3


@dataclass(frozen=True)
class Issue:
    code: str
    piece: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} [{self.piece}] {self.detail}"


def _bodies(m: Manifold) -> int:
    """Connected solids; closed inner pockets (negative volume) do not count."""
    return sum(1 for part in m.decompose() if part.volume() > 0)


def _overhangs(solid: Manifold, max_bridge: float) -> list[float]:
    """Regions facing down above the first layer that no bridge can span.

    A region is fine when every point lies within half a bridge length of its edge, like the
    floor of an engraving on the bed face. Returns the widths of the regions that fail.
    """
    verts, tris = mesh_arrays(solid)
    corners = verts[tris].astype(np.float64)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    nz = normals[:, 2] / np.maximum(lengths, 1e-12)
    down = (nz < OVERHANG_COS) & (corners[:, :, 2].min(axis=1) > 0.3) & (lengths > 1e-9)
    idx = np.nonzero(down)[0]
    if len(idx) == 0:
        return []
    # Union-find over shared vertices.
    parent = {int(i): int(i) for i in idx}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    by_vertex: dict[int, int] = {}
    for i in idx:
        for v in tris[i]:
            other = by_vertex.setdefault(int(v), int(i))
            ra, rb = find(int(i)), find(other)
            if ra != rb:
                parent[ra] = rb
    groups: dict[int, list[int]] = {}
    for i in idx:
        groups.setdefault(find(int(i)), []).append(int(i))
    failed: list[float] = []
    for members in groups.values():
        flat = [[(float(x), float(y)) for x, y, _ in corners[m]] for m in members]
        region = CrossSection.batch_boolean(
            [polygons([t]) for t in flat if abs(_area2(t)) > 1e-9], OpType.Add
        )
        if not region.offset(-max_bridge / 2).is_empty():
            x0, y0, x1, y1 = bounds(region)
            failed.append(max(x1 - x0, y1 - y0))
    return failed


def _area2(t: list[tuple[float, float]]) -> float:
    (ax, ay), (bx, by), (cx, cy) = t
    return (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)


def _thin_area(section: CrossSection, width: float) -> float:
    """Area of material thinner than ``width`` (morphological opening)."""
    if section.is_empty():
        return 0.0
    opened = section.offset(-width / 2).offset(width / 2)
    # Sharp corners lose their tips in the opening; only count real slivers.
    return sum(part.area() for part in (section - opened).decompose() if part.area() > 0.25)


def check_piece(p: Piece) -> list[Issue]:
    issues: list[Issue] = []
    solid = p.printed(p.solid)
    if solid.status().name != "NoError":
        issues.append(Issue("invalid", p.key, solid.status().name))
        return issues
    if _bodies(solid) != 1:
        issues.append(Issue("pieces", p.key, f"{_bodies(solid)} separate bodies"))
    x0, y0, z0, x1, y1, z1 = bbox(solid)
    if max(x1 - x0, y1 - y0, z1 - z0) > MAX_PRINT:
        issues.append(Issue("too_big", p.key, f"{x1 - x0:.0f} x {y1 - y0:.0f} x {z1 - z0:.0f}"))
    if abs(z0) > 1e-6:
        issues.append(Issue("not_on_bed", p.key, f"z={z0:.3f}"))
    if solid.slice(0.1).area() < 50.0:
        issues.append(Issue("bed_contact", p.key, f"{solid.slice(0.1).area():.0f} mm²"))
    allowed = 26.0 if p.key == "figure" else MAX_BRIDGE  # closed tag pocket: print pause
    for span in _overhangs(solid, allowed):
        issues.append(Issue("overhang", p.key, f"unsupported region {span:.1f} mm wide"))
    z = 1.0
    while z < z1 - 0.5:
        area = _thin_area(solid.slice(z), MIN_FEATURE)
        if area > 2.0:
            issues.append(
                Issue("thin", p.key, f"{area:.1f} mm² thinner than {MIN_FEATURE} mm at z={z:.1f}")
            )
            break
        z += 1.0
    return issues


def check_assembly(model: CaseModel) -> list[Issue]:
    issues: list[Issue] = []
    solids = {p.key: p.solid + p.inlay if not p.inlay.is_empty() else p.solid for p in model.pieces}
    for c in model.components:
        for key, solid in solids.items():
            if key == "figure":
                continue
            if key in c.mounts:
                overlap = (solid ^ c.solid).volume()
                if overlap > 0.01:
                    issues.append(
                        Issue("collision", key, f"{c.key} penetrates by {overlap:.2f} mm³")
                    )
            else:
                gap = solid.min_gap(c.solid, 2.0)
                if gap < MIN_GAP:
                    issues.append(Issue("clearance", key, f"{c.key}: {gap:.2f} mm"))
    comps = list(model.components)
    for i, a in enumerate(comps):
        for b in comps[i + 1 :]:
            if a.key.split("_")[0] == b.key.split("_")[0] and a.key.startswith("board"):
                continue
            gap = a.solid.min_gap(b.solid, 2.0)
            if gap < MIN_GAP:
                issues.append(Issue("clearance", a.key, f"{b.key}: {gap:.2f} mm"))
    for plug in model.plug_paths:
        for key, solid in solids.items():
            overlap = (solid ^ plug).volume()
            if overlap > 0.01:
                issues.append(Issue("plug_blocked", key, f"{overlap:.2f} mm³"))
    issues += _check_nfc(model)
    issues += _check_grille(model)
    return issues


def _check_nfc(model: CaseModel) -> list[Issue]:
    lay = model.layout
    fx, fy = lay.figure
    w, d = PN532
    top = lay.height - TOP
    antenna = top - PCB
    if lay.height - antenna > 4.0:
        return [Issue("nfc_depth", "body", f"antenna {lay.height - antenna:.1f} mm below the top")]
    zone = box(
        fx - w / 2 - NFC_MARGIN, fy - d / 2 - NFC_MARGIN, antenna - NFC_DEPTH,
        fx + w / 2 + NFC_MARGIN, fy + d / 2 + NFC_MARGIN, antenna,
    )  # fmt: skip
    issues: list[Issue] = []
    for c in model.components:
        if c.key == "nfc" or c.key.startswith("button"):
            continue
        overlap = (zone ^ c.solid).volume()
        if overlap > 0.01:
            issues.append(Issue("nfc_zone", c.key, f"{overlap:.0f} mm³ inside the NFC field"))
    return issues


def _check_grille(model: CaseModel) -> list[Issue]:
    cfg = model.config
    radius = cfg.speaker / 2 - 2.0
    holes = patterns.grille(cfg.grille, radius)
    issues: list[Issue] = []
    ratio = holes.area() / (np.pi * radius * radius)
    if ratio < MIN_OPEN_AREA:
        issues.append(Issue("grille_closed", "front", f"{ratio:.0%} open"))
    if not holes.offset(-patterns.MAX_HOLE / 2).is_empty():
        issues.append(Issue("grille_wide", "front", f"openings wider than {patterns.MAX_HOLE} mm"))
    if _thin_area(CrossSection.circle(radius + 3.0) - holes, MIN_FEATURE) > 1.0:
        issues.append(Issue("grille_thin", "front", f"bars thinner than {MIN_FEATURE} mm"))
    return issues


def check(model: CaseModel) -> list[Issue]:
    issues: list[Issue] = []
    for p in model.pieces:
        issues += check_piece(p)
    return issues + check_assembly(model)
