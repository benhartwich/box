"""What a person can choose in "Box gestalten". Frozen and canonical, so equal choices give equal
files and digests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

from myboxi_case import GENERATOR_VERSION, text

MAX_NAME = 14

Form = Literal["radio", "cube", "bear"]
Board = Literal["zero2w", "pi4"]
Power = Literal["usbc", "powerbank"]
Grille = Literal["dots", "stars", "hearts", "lines"]
Colors = Literal["multi", "mono"]
Fastening = Literal["self_tap", "insert"]

# Filament-like colours: key -> (label, sRGB hex). Keys are part of URLs and files.
PALETTE: dict[str, tuple[str, str]] = {
    "creme": ("Creme", "#F3EAD7"),
    "sand": ("Sand", "#E3C79F"),
    "apricot": ("Apricot", "#E0874F"),
    "sonne": ("Sonnengelb", "#F2C66D"),
    "moos": ("Moosgrün", "#2E6B5C"),
    "salbei": ("Salbei", "#9BC7B5"),
    "himmel": ("Himmelblau", "#9CC9E6"),
    "flieder": ("Flieder", "#C4B2E0"),
    "rosa": ("Rosa", "#F2B8C6"),
    "rot": ("Rot", "#D64541"),
    "braun": ("Braun", "#8B5E3C"),
    "anthrazit": ("Anthrazit", "#3A3F3E"),
    "weiss": ("Weiß", "#FAFAF7"),
}
ColorKey = Literal[
    "creme", "sand", "apricot", "sonne", "moos", "salbei", "himmel", "flieder", "rosa", "rot",
    "braun", "anthrazit", "weiss",
]  # fmt: skip


def _squash(value: object) -> object:
    return " ".join(value.split()) if isinstance(value, str) else value


def _name(value: str) -> str:
    missing = text.supported(value)
    if missing:
        chars = " ".join(sorted(missing))
        raise ValueError(f"Diese Zeichen gibt es in der Schrift nicht: {chars}")
    return value


class CaseConfig(BaseModel):
    """One case configuration. The case is outside the device protocol (docs/gehaeuse.md)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    form: Form = "radio"
    board: Board = "zero2w"
    power: Power = "usbc"
    name: Annotated[
        str, BeforeValidator(_squash), Field(max_length=MAX_NAME), AfterValidator(_name)
    ] = ""
    grille: Grille = "dots"
    speaker: Literal[40, 50, 57] = 40
    button: Literal[16, 24] = 16
    colors: Colors = "multi"
    color_body: ColorKey = "sand"
    color_front: ColorKey = "moos"
    color_accent: ColorKey = "creme"
    tolerance: Annotated[float, Field(ge=0.1, le=0.4)] = 0.2
    fastening: Fastening = "self_tap"

    @field_validator("tolerance")
    @classmethod
    def _tolerance_steps(cls, value: float) -> float:
        return round(round(value / 0.05) * 0.05, 2)

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> Self:
        """Parse URL query values (strings); unknown keys are rejected."""
        data: dict[str, object] = {}
        for key, value in query.items():
            if key in ("speaker", "button"):
                data[key] = int(value) if value.isdigit() else value
            elif key == "tolerance":
                try:
                    data[key] = float(value)
                except ValueError:
                    data[key] = value
            else:
                data[key] = value
        return cls.model_validate(data)

    def query(self) -> dict[str, str]:
        """Only the choices that differ from the defaults, as URL query values."""
        default = CaseConfig()
        out: dict[str, str] = {}
        for key in type(self).model_fields:
            value = getattr(self, key)
            if value != getattr(default, key):
                out[key] = str(value)
        return out

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        data = f"{GENERATOR_VERSION}\n{self.canonical_json()}".encode()
        return hashlib.sha256(data).hexdigest()

    def color(self, role: Literal["body", "front", "accent"]) -> str:
        key = {"body": self.color_body, "front": self.color_front, "accent": self.color_accent}
        return PALETTE[key[role]][1]
