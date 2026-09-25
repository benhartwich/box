"""Small software renderer (numpy z-buffer) for PNG previews without a GPU.

Used for tests, CI artefacts and the landing page; the web configurator renders with WebGL.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from manifold3d import Manifold

from myboxi_case.build import CaseModel
from myboxi_case.geom import mesh_arrays

Floats = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Item:
    solid: Manifold
    color: str  # "#rrggbb"
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)


def _rgb(color: str) -> Floats:
    return np.array([int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)], dtype=np.float64)


def _view(yaw: float, pitch: float) -> Floats:
    """Rotation from world to camera (camera looks along -z)."""
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    turn = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])  # about z
    # World y (depth) points away from the viewer, z up: map to camera x right, y up, z towards.
    to_cam = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    tilt = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])  # look down by pitch
    return tilt @ to_cam @ turn


def render(
    items: Sequence[Item], size: tuple[int, int] = (800, 600), yaw: float = -30.0,
    pitch: float = 22.0, background: str = "#f6f1e7", supersample: int = 2,
) -> bytes:  # fmt: skip
    """PNG bytes of ``items`` in an orthographic three-quarter view from the front right."""
    w, h = size[0] * supersample, size[1] * supersample
    rot = _view(yaw, pitch)
    tris: list[Floats] = []
    colors: list[Floats] = []
    for item in items:
        if item.solid.is_empty():
            continue
        verts32, faces32 = mesh_arrays(item.solid)
        verts = verts32.astype(np.float64) + np.array(item.offset)
        faces = faces32.astype(np.int64)
        tris.append(verts[faces] @ rot.T)
        colors.append(np.repeat(_rgb(item.color)[None, :], len(faces), axis=0))
    if not tris:
        raise ValueError("nothing to render")
    tri = np.concatenate(tris)  # (n, 3, 3) in camera space
    col = np.concatenate(colors)

    lo = tri.reshape(-1, 3).min(axis=0)
    hi = tri.reshape(-1, 3).max(axis=0)
    scale = 0.9 * min(w / (hi[0] - lo[0]), h / (hi[1] - lo[1]))
    centre = (lo + hi) / 2
    sx = (tri[:, :, 0] - centre[0]) * scale + w / 2
    sy = h / 2 - (tri[:, :, 1] - centre[1]) * scale
    depth = tri[:, :, 2]

    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = (lengths > 1e-12) & (normals[:, 2] > 0)  # facing the camera
    normals = normals / np.maximum(lengths, 1e-12)[:, None]
    light = np.array([0.35, 0.55, 0.76])
    light = light / np.linalg.norm(light)
    diffuse = np.clip(normals @ light, 0, 1)
    shade = 0.46 + 0.46 * diffuse + 0.12 * normals[:, 1].clip(0, 1)
    shaded = np.clip(col * shade[:, None], 0, 1)

    image = np.empty((h, w, 3))
    image[:] = _rgb(background)
    zbuf = np.full((h, w), -np.inf)
    for i in np.nonzero(keep)[0]:
        x = sx[i]
        y = sy[i]
        x0, x1 = max(int(x.min()), 0), min(int(x.max()) + 1, w - 1)
        y0, y1 = max(int(y.min()), 0), min(int(y.max()) + 1, h - 1)
        if x0 > x1 or y0 > y1:
            continue
        px, py = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        d = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
        if abs(d) < 1e-12:
            continue
        a = ((y[1] - y[2]) * (px - x[2]) + (x[2] - x[1]) * (py - y[2])) / d
        b = ((y[2] - y[0]) * (px - x[2]) + (x[0] - x[2]) * (py - y[2])) / d
        c = 1 - a - b
        inside = (a >= -1e-6) & (b >= -1e-6) & (c >= -1e-6)
        if not inside.any():
            continue
        z = a * depth[i, 0] + b * depth[i, 1] + c * depth[i, 2]
        region = zbuf[y0 : y1 + 1, x0 : x1 + 1]
        closer = inside & (z > region)
        region[closer] = z[closer]
        image[y0 : y1 + 1, x0 : x1 + 1][closer] = shaded[i]

    # Darken silhouettes and creases a little: reads better at small sizes.
    edge = np.zeros((h, w), dtype=bool)
    finite = np.isfinite(zbuf)
    zf = np.where(finite, zbuf, -1e6)
    edge[:, 1:] |= np.abs(np.diff(zf, axis=1)) > 3.0
    edge[1:, :] |= np.abs(np.diff(zf, axis=0)) > 3.0
    image[edge] *= 0.55

    small = image.reshape(size[1], supersample, size[0], supersample, 3).mean(axis=(1, 3))
    return _png((small * 255 + 0.5).astype(np.uint8))


def _png(pixels: npt.NDArray[np.uint8]) -> bytes:
    h, w, _ = pixels.shape
    raw = b"".join(b"\x00" + pixels[row].tobytes() for row in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def assembled(model: CaseModel, *, inside: bool = False, explode: float = 0.0) -> list[Item]:
    """The case as it stands on the table; ``inside`` shows the bought parts instead of the body."""
    items: list[Item] = []
    for p in model.pieces:
        if inside and p.key in ("body", "front"):
            continue
        off = tuple(explode * c for c in p.explode)
        offset = (off[0], off[1], off[2])
        items.append(Item(p.solid, p.color, offset))
        if not p.inlay.is_empty():
            items.append(Item(p.inlay, p.inlay_color, offset))
    if inside:
        items += [Item(c.solid, c.color) for c in model.components]
    return items
