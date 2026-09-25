"""Small geometry helpers on top of manifold3d (millimetres, assembly frame, see __init__)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
from manifold3d import (
    CrossSection,
    FillRule,
    Manifold,
    OpType,
    set_min_circular_angle,
    set_min_circular_edge_length,
)

# Smooth enough for 0.2 mm layers, still small meshes for the preview.
set_min_circular_angle(6.0)
set_min_circular_edge_length(0.6)

EMPTY = Manifold()

Box3 = tuple[float, float, float, float, float, float]
Box2 = tuple[float, float, float, float]
Points = Sequence[tuple[float, float]]


# Typed access to manifold3d, whose stubs leave these return types open.
def bbox(m: Manifold) -> Box3:
    """(x0, y0, z0, x1, y1, z1)"""
    raw: Any = m
    v = [float(x) for x in raw.bounding_box()]
    return (v[0], v[1], v[2], v[3], v[4], v[5])


def bounds(section: CrossSection) -> Box2:
    """(x0, y0, x1, y1)"""
    raw: Any = section
    v = [float(x) for x in raw.bounds()]
    return (v[0], v[1], v[2], v[3])


def mesh_arrays(m: Manifold) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint32]]:
    """Vertex positions (n, 3) and triangle indices (m, 3) of ``m``."""
    mesh: Any = m.to_mesh()
    verts = np.ascontiguousarray(np.asarray(mesh.vert_properties, dtype=np.float32)[:, :3])
    tris = np.ascontiguousarray(np.asarray(mesh.tri_verts, dtype=np.uint32))
    return verts, tris


def polygons(contours: Iterable[Points], fill: FillRule = FillRule.Positive) -> CrossSection:
    arrays: Any = [np.asarray(c, dtype=np.float64) for c in contours]
    return CrossSection(arrays, fill)


def union(items: Iterable[Manifold]) -> Manifold:
    parts = [m for m in items if not m.is_empty()]
    if not parts:
        return Manifold()
    return Manifold.batch_boolean(parts, OpType.Add)


def section_union(items: Iterable[CrossSection]) -> CrossSection:
    parts = list(items)
    if not parts:
        return CrossSection()
    return CrossSection.batch_boolean(parts, OpType.Add)


def circle(r: float, x: float = 0.0, y: float = 0.0) -> CrossSection:
    return CrossSection.circle(r).translate((x, y))


def rect(x0: float, y0: float, x1: float, y1: float) -> CrossSection:
    return CrossSection.square((x1 - x0, y1 - y0)).translate((x0, y0))


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> CrossSection:
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    if r <= 0:
        return rect(x0, y0, x1, y1)
    return CrossSection.batch_hull(
        [circle(r, x, y) for x in (x0 + r, x1 - r) for y in (y0 + r, y1 - r)]
    )


def profile(width: float, depth: float, r_front: float, r_back: float) -> CrossSection:
    """Convex footprint in x/y: front corners (y=0) with ``r_front``, back with ``r_back``."""
    return CrossSection.batch_hull(
        [
            circle(r_front, r_front, r_front),
            circle(r_front, width - r_front, r_front),
            circle(r_back, r_back, depth - r_back),
            circle(r_back, width - r_back, depth - r_back),
        ]
    )


def prism(section: CrossSection, z0: float, z1: float) -> Manifold:
    """``section`` (drawn in x/y) as a solid between heights z0 and z1."""
    return section.extrude(z1 - z0).translate((0, 0, z0))


def chamfered_prism(section: CrossSection, height: float, chamfer: float) -> Manifold:
    """Convex prism from z=0 to ``height`` with a 45° chamfer along the top edge."""
    body = section.extrude(height - chamfer)
    top = section.offset(-chamfer).extrude(chamfer).translate((0, 0, height - chamfer))
    return Manifold.batch_hull([body, top.translate((0, 0, 0))])


def xz_slab(section: CrossSection, y0: float, y1: float) -> Manifold:
    """``section`` drawn in x/z (as its x/y) as a solid between y0 and y1."""
    # rotate((90, 0, 0)) maps (u, v, w) to (u, -w, v): extrude along w, then move into place.
    return section.extrude(y1 - y0).rotate((90, 0, 0)).translate((0, y1, 0))


def yz_slab(section: CrossSection, x0: float, x1: float) -> Manifold:
    """``section`` drawn in y/z (as its x/y) as a solid between x0 and x1."""
    # rotate((90, 0, 90)) maps (u, v, w) to (w, u, v).
    return section.extrude(x1 - x0).rotate((90, 0, 90)).translate((x0, 0, 0))


def cylinder_z(r: float, x: float, y: float, z0: float, z1: float) -> Manifold:
    return Manifold.cylinder(z1 - z0, r).translate((x, y, z0))


def cylinder_y(r: float, x: float, z: float, y0: float, y1: float) -> Manifold:
    return xz_slab(circle(r, x, z), y0, y1)


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def polygon(points: Iterable[tuple[float, float]]) -> CrossSection:
    return polygons([list(points)])


def regular(n: int, r: float, rotation: float = 90.0) -> list[tuple[float, float]]:
    return [
        (
            r * math.cos(math.radians(rotation + 360 * i / n)),
            r * math.sin(math.radians(rotation + 360 * i / n)),
        )
        for i in range(n)
    ]
