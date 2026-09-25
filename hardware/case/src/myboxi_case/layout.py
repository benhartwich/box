"""Fixed layouts per form, board and power option; shared wall and fit dimensions (mm).

Positions are chosen by hand and verified by checks.py (clearances, NFC range, printability).
Combinations without a checked layout are refused with a German message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from myboxi_case.config import CaseConfig, Form

WALL = 2.4  # side and back walls: 6 perimeters with a 0.4 mm nozzle
TOP = 2.0  # top plate above the NFC antenna: thin enough for the reader's range
BASE = 3.0  # base plate
CHAMFER = 2.0  # 45° instead of a fillet on edges that lie on the print bed
PANEL_T = 3.0  # front panel thickness
RAIL_T = 2.0
RAIL_OVERLAP = 3.0
PANEL_OVERLAP = 3.0  # how far the front panel reaches behind the window frame
WINDOW_R = 6.0
ENGRAVE = 0.6  # three 0.2 mm layers: readable, and one colour change for two-colour prints
RING_R = 22.5  # figure spot on top (engraved ring)
RING_W = 1.2
COLUMN_R = 4.0
STANDOFF_H = 5.0
STANDOFF_R = 3.0
SPEAKER_RIM = 3.0  # thickness of the speaker frame that the clamp ring presses
SPEAKER_DEPTH = 22.0
SOCKET_HOLE_DX = 12.0  # screw holes of the USB-C panel socket, from its centre (measure!)

Action = Literal["play_pause", "volume_up", "volume_down", "next"]
Symbol = Literal["play_pause", "plus", "minus", "next"]
SYMBOLS: dict[Action, Symbol] = {
    "play_pause": "play_pause",
    "volume_up": "plus",
    "volume_down": "minus",
    "next": "next",
}


class LayoutError(ValueError):
    """A combination that does not fit; the message is shown to people (German)."""


@dataclass(frozen=True)
class Button:
    action: Action
    x: float
    y: float
    symbol_dy: float  # symbol position relative to the button (front: negative)


@dataclass(frozen=True)
class BoardPlace:
    x: float  # board corner with the lowest x and y after rotation
    y: float
    rotation: Literal[0, 90, 180, 270]  # 0: long side along x, ports facing -y


@dataclass(frozen=True)
class Layout:
    form: Form
    width: float
    depth: float
    height: float
    r_front: float
    r_back: float
    frame_top: float  # window frame above the front panel
    figure: tuple[float, float]  # ring centre on top (x, y)
    buttons: tuple[Button, ...]
    speaker: tuple[float, float]  # centre on the front panel (x, z)
    name_box: tuple[float, float, float, float]  # x0, z0, x1, z1 on the front panel
    board: BoardPlace
    amp: tuple[float, float]  # centre of the amplifier groove (x, y); board stands along x
    socket: tuple[float, float]  # USB-C socket in the back wall (x, z)
    powerbank: tuple[float, float] | None  # corner (x, y) of the compartment
    ears: bool = False
    face: bool = False

    @property
    def ri_front(self) -> float:
        return self.r_front - WALL

    @property
    def panel_x(self) -> tuple[float, float]:
        """Front panel extent: behind the window frame, clear of the rounded front corners."""
        return (WALL + self.ri_front, self.width - WALL - self.ri_front)

    @property
    def window_x(self) -> tuple[float, float]:
        x0, x1 = self.panel_x
        return (x0 + PANEL_OVERLAP, x1 - PANEL_OVERLAP)

    @property
    def window_top(self) -> float:
        return self.height - self.frame_top


def _cube_buttons(width: float) -> tuple[Button, ...]:
    return (
        Button("volume_down", 19.0, 35.0, -14.0),
        Button("volume_up", width - 19.0, 35.0, -14.0),
        Button("play_pause", 19.0, 80.0, 14.0),
        Button("next", width - 19.0, 80.0, 14.0),
    )


def _cube(cfg: CaseConfig) -> Layout:
    w = d = 110.0
    h = 100.0
    if cfg.power == "powerbank":
        raise LayoutError("Eine Powerbank passt nur ins Radio (mit Pi Zero 2 W).")
    if cfg.button != 16:
        raise LayoutError("In Würfel und Bär passen nur 16-mm-Taster.")
    if cfg.speaker != 40:
        raise LayoutError("In Würfel und Bär passt ein Lautsprecher mit 40 mm Durchmesser.")
    bear = cfg.form == "bear"
    board = BoardPlace(22.5, 50.0, 180) if cfg.board == "zero2w" else BoardPlace(12.5, 40.0, 0)
    return Layout(
        form=cfg.form,
        width=w,
        depth=d,
        height=h,
        r_front=12.0,
        r_back=12.0,
        frame_top=15.0,
        figure=(w / 2, 58.0),
        buttons=_cube_buttons(w),
        speaker=(w / 2, 50.0 if bear else 56.0),
        name_box=(22.0, 9.0 if bear else 11.0, w - 22.0, 21.0 if bear else 27.0),
        board=board,
        amp=(24.0, 31.0) if cfg.board == "zero2w" else (60.0, 32.0),
        socket=(75.0, 14.0) if cfg.board == "zero2w" else (30.0, 46.0),
        powerbank=None,
        ears=bear,
        face=bear,
    )


def _radio(cfg: CaseConfig) -> Layout:
    w, d, h = 160.0, 90.0, 100.0
    if cfg.power == "powerbank" and cfg.board == "pi4":
        raise LayoutError("Pi 4 und Powerbank passen nicht gemeinsam ins Radio.")
    buttons = (
        Button("volume_down", 110.0, 28.0, -14.0),
        Button("volume_up", 140.0, 28.0, -14.0),
        Button("play_pause", 110.0, 62.0, 14.0),
        Button("next", 140.0, 62.0, 14.0),
    )
    if cfg.button == 24:
        buttons = (
            Button("volume_down", 100.0, 26.0, -18.0),
            Button("volume_up", 136.0, 26.0, -18.0),
            Button("play_pause", 100.0, 64.0, 18.0),
            Button("next", 136.0, 64.0, 18.0),
        )
    powerbank = cfg.power == "powerbank"
    if cfg.board == "pi4":
        board, amp = BoardPlace(68.5, 20.5, 0), (22.0, 34.0)
    elif powerbank:
        board, amp = BoardPlace(110.0, 12.0, 90), (88.0, 11.0)
    else:
        board, amp = BoardPlace(80.0, 45.0, 180), (86.0, 33.0)
    return Layout(
        form="radio",
        width=w,
        depth=d,
        height=h,
        r_front=6.0,
        r_back=14.0,
        frame_top=12.0,
        figure=(46.0, 45.0),
        buttons=buttons,
        speaker=(42.0, 52.0),
        name_box=(88.0, 34.0, 146.0, 62.0),
        board=board,
        amp=amp,
        socket=(40.0, 40.0 if powerbank else 14.0),
        powerbank=(13.5, 20.0) if powerbank else None,
    )


def layout_for(cfg: CaseConfig) -> Layout:
    if cfg.form == "radio":
        return _radio(cfg)
    return _cube(cfg)
