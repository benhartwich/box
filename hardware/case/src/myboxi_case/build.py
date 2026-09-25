"""Build a complete case: printed parts, bought components and print orientation."""

from __future__ import annotations

from dataclasses import dataclass, field

from manifold3d import Manifold

from myboxi_case import components, parts
from myboxi_case.components import Component
from myboxi_case.config import CaseConfig
from myboxi_case.geom import bbox
from myboxi_case.layout import Layout, layout_for

# Tool heads of a multi-colour printer (e.g. Snapmaker U1): body, front, accent.
TOOL_BODY, TOOL_FRONT, TOOL_ACCENT = 1, 2, 3


@dataclass(frozen=True)
class Piece:
    """One printable object: the part plus, for multi-colour prints, flush inlays."""

    key: str
    label: str
    solid: Manifold  # assembly frame
    color: str
    tool: int
    rotation: tuple[float, float, float]  # to print orientation (degrees, applied x, y, z)
    explode: tuple[float, float, float]  # direction for the exploded preview
    inlay: Manifold = field(default_factory=Manifold)
    inlay_color: str = ""
    inlay_tool: int = TOOL_ACCENT

    def printed(self, m: Manifold) -> Manifold:
        """``m`` (this piece or its inlay) turned into print orientation, resting on z=0."""
        turned = self.solid.rotate(self.rotation)
        x0, y0, z0, _, _, _ = bbox(turned)
        return m.rotate(self.rotation).translate((-x0, -y0, -z0))


@dataclass(frozen=True)
class CaseModel:
    config: CaseConfig
    layout: Layout
    pieces: tuple[Piece, ...]
    components: tuple[Component, ...]
    plug_paths: tuple[Manifold, ...]  # must stay free of the case (USB-C plug from outside)

    def piece(self, key: str) -> Piece:
        return next(p for p in self.pieces if p.key == key)


def build(cfg: CaseConfig) -> CaseModel:
    """Raises layout.LayoutError for combinations that do not fit."""
    layout = layout_for(cfg)
    g = parts.Geometry(cfg, layout)
    multi = cfg.colors == "multi"
    body_c, front_c, accent_c = cfg.color("body"), cfg.color("front"), cfg.color("accent")

    def piece(
        key: str,
        label: str,
        shape: parts.Shape,
        color: str,
        tool: int,
        rotation: tuple[float, float, float],
        explode: tuple[float, float, float],
    ) -> Piece:
        if multi and not shape.inlay.is_empty():
            return Piece(
                key, label, shape.solid, color, tool, rotation, explode, shape.inlay, accent_c
            )
        # Single colour: the engraving stays empty, readable by its shadow.
        return Piece(key, label, shape.solid, color, tool, rotation, explode)

    pieces = [
        piece("body", "Korpus", parts.body(g), body_c, TOOL_BODY, (180, 0, 0), (0, 0, 1)),
        piece("front", "Front", parts.front(g), front_c, TOOL_FRONT, (90, 0, 0), (0, -1, 0)),
        piece("base", "Boden", parts.base(g), body_c, TOOL_BODY, (0, 0, 0), (0, 0, -1)),
        piece(
            "speaker_ring",
            "Lautsprecherring",
            parts.speaker_ring(g),
            front_c,
            TOOL_FRONT,
            (90, 0, 0),
            (0, 0.6, 0),
        ),
    ]
    if layout.ears:
        for i, (x, y) in enumerate(parts.ear_positions(layout)):
            key = ("ear_left", "ear_right")[i]
            pieces.append(
                piece(key, "Ohr", parts.ear(g, x, y), body_c, TOOL_BODY, (90, 0, 0), (0, 0, 1.4))
            )
    figure = parts.figure_base(g)
    pieces.append(
        piece(
            "figure",
            "Figurensockel",
            figure,
            accent_c if multi else body_c,
            TOOL_ACCENT if multi else TOOL_BODY,
            (0, 0, 0),
            (0, 0, 1),
        )
    )

    comps: list[Component] = [
        *components.board(layout, cfg.board),
        components.pn532(layout),
        components.speaker(layout, cfg.speaker),
        *components.buttons(layout, cfg.button),
        components.amp(layout),
    ]
    socket, plug = components.socket(layout)
    comps.append(socket)
    bank = components.powerbank(layout)
    if bank is not None:
        comps.append(bank)
    return CaseModel(cfg, layout, tuple(pieces), tuple(comps), (plug,))
