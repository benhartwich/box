"""Case generator: configuration, geometry checks over every supported combination, exports."""

from __future__ import annotations

import io
import itertools
import json
import struct
import zipfile
from dataclasses import replace
from xml.etree import ElementTree as ET

import numpy as np
import pytest
from pydantic import ValidationError

from myboxi_case import GENERATOR_VERSION, export
from myboxi_case.build import build
from myboxi_case.checks import check, check_assembly
from myboxi_case.components import Component
from myboxi_case.config import CaseConfig
from myboxi_case.geom import bbox, mesh_arrays
from myboxi_case.layout import LayoutError
from myboxi_case.render import assembled, render

NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
LAYOUTS = [
    ("radio", "zero2w", "usbc"),
    ("radio", "zero2w", "powerbank"),
    ("radio", "pi4", "usbc"),
    ("cube", "zero2w", "usbc"),
    ("cube", "pi4", "usbc"),
    ("bear", "zero2w", "usbc"),
    ("bear", "pi4", "usbc"),
    *[
        (form, board, "usbc")
        for form in ("unicorn", "cat", "bunny", "frog")
        for board in ("zero2w", "pi4")
    ],
]


# --- configuration ------------------------------------------------------------------------------


def test_query_round_trip_and_defaults() -> None:
    cfg = CaseConfig(form="bear", name="Mia", color_body="braun", tolerance=0.25)
    assert cfg.query() == {
        "form": "bear",
        "name": "Mia",
        "color_body": "braun",
        "tolerance": "0.25",
    }
    assert CaseConfig.from_query(cfg.query()) == cfg
    assert CaseConfig().query() == {}


def test_invalid_choices_are_refused() -> None:
    with pytest.raises(ValidationError):
        CaseConfig.from_query({"form": "castle"})
    with pytest.raises(ValidationError):
        CaseConfig.from_query({"unknown": "x"})
    with pytest.raises(ValidationError):
        CaseConfig(name="x" * 15)
    with pytest.raises(ValidationError, match="Zeichen"):
        CaseConfig(name="Mia 🐻")
    assert CaseConfig(name="  Łukasz   Ölçü ").name == "Łukasz Ölçü"
    assert CaseConfig(tolerance=0.23).tolerance == 0.25


def test_digest_depends_on_choices_and_generator() -> None:
    a, b = CaseConfig(name="Mia"), CaseConfig(name="Mio")
    assert a.digest() == CaseConfig(name="Mia").digest() != b.digest()
    assert len(a.digest()) == 64


@pytest.mark.parametrize(
    ("form", "board", "power", "button", "speaker"),
    [
        ("cube", "zero2w", "powerbank", 16, 40),
        ("radio", "pi4", "powerbank", 16, 40),
        ("bear", "zero2w", "usbc", 24, 40),
        ("cube", "zero2w", "usbc", 16, 50),
    ],
)
def test_unfitting_combinations_are_refused(
    form: str, board: str, power: str, button: int, speaker: int
) -> None:
    cfg = CaseConfig.model_validate(
        {"form": form, "board": board, "power": power, "button": button, "speaker": speaker}
    )
    with pytest.raises(LayoutError):
        build(cfg)


# --- geometry -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("form", "board", "power"), LAYOUTS)
def test_every_option_passes_the_checks(form: str, board: str, power: str) -> None:
    """Printable without supports, room for every bought part, NFC field and plug path free."""
    for grille, speaker, button in itertools.product(
        ("dots", "stars", "hearts", "lines"), (40, 50, 57), (16, 24)
    ):
        cfg = CaseConfig.model_validate(
            {"form": form, "board": board, "power": power, "grille": grille, "speaker": speaker,
             "button": button, "name": "Großmama Ölçü"}
        )  # fmt: skip
        try:
            model = build(cfg)
        except LayoutError:
            continue
        assert check(model) == [], cfg.query()


def test_checks_find_a_collision() -> None:
    model = build(CaseConfig())
    speaker = next(c for c in model.components if c.key == "speaker")
    intruder = Component("extra", "Fremdkörper", speaker.solid.translate((0, 3, 0)), (), "#000000")
    broken = replace(model, components=(*model.components, intruder))
    codes = {i.code for i in check_assembly(broken)}
    assert "clearance" in codes


def test_parts_are_single_watertight_bodies() -> None:
    model = build(CaseConfig(form="bear", name="Mia"))
    keys = [p.key for p in model.pieces]
    assert keys == ["body", "front", "base", "speaker_ring", "ear_left", "ear_right", "figure"]
    for p in model.pieces:
        solid = p.printed(p.solid)
        assert solid.status().name == "NoError"
        assert bbox(solid)[2] == pytest.approx(0.0, abs=1e-6)
    front = model.piece("front")
    assert not front.inlay.is_empty()  # name, eyes and nose as a second colour


def test_unicorn_has_a_horn_in_the_accent_colour() -> None:
    model = build(CaseConfig(form="unicorn", color_accent="sonne"))
    horn = model.piece("horn")
    assert horn.color == "#F2C66D"
    assert horn.tool == 3
    box = bbox(horn.printed(horn.solid))
    assert box[5] - box[2] == pytest.approx(40.0, abs=0.01)  # stands on its base


def test_single_colour_has_no_inlays() -> None:
    model = build(CaseConfig(name="Mia", colors="mono"))
    assert all(p.inlay.is_empty() for p in model.pieces)


# --- exports ------------------------------------------------------------------------------------


def test_stl_is_binary_and_complete() -> None:
    model = build(CaseConfig())
    p = model.piece("base")
    data = export.stl(p.printed(p.solid), "Boden")
    (count,) = struct.unpack("<I", data[80:84])
    assert count == p.solid.num_tri()
    assert len(data) == 84 + 50 * count


def test_3mf_assigns_tool_heads_and_colours() -> None:
    model = build(CaseConfig(form="bear", name="Mia", color_accent="anthrazit"))
    plates = export.threemf(model)
    assert len(plates) == 1
    zf = zipfile.ZipFile(io.BytesIO(plates[0]))
    root = ET.fromstring(zf.read("3D/3dmodel.model"))  # noqa: S314 - our own output
    colours = [b.get("displaycolor") for b in root.iterfind(".//m:basematerials/m:base", NS)]
    assert "#3A3F3EFF" in colours
    build_items = root.findall("m:build/m:item", NS)
    assert len(build_items) == len(model.pieces)
    config = ET.fromstring(zf.read("Metadata/model_settings.config"))  # noqa: S314
    tools: dict[str, str] = {}
    for part in config.iter("part"):
        meta = {m.get("key"): m.get("value") for m in part.iter("metadata")}
        tools[str(meta["name"])] = str(meta["extruder"])
    assert tools["Korpus"] == "1"
    assert tools["Front"] == "2"
    assert tools["Front Einlage"] == "3"
    # Every part lies on the plate, clear of the prime tower corner.
    for placed in export.arrange(model.pieces):
        x0, y0, z0, x1, y1, _ = bbox(placed.solid)
        assert z0 == pytest.approx(0.0, abs=1e-6)
        assert x0 >= 5.0
        assert y0 >= 5.0
        assert x1 <= 265.0
        assert y1 <= 265.0
        assert not (x0 < 72.0 and y1 > 190.0)


def test_radio_needs_two_plates() -> None:
    assert len(export.threemf(build(CaseConfig(form="radio")))) == 2


def test_exports_are_deterministic() -> None:
    cfg = CaseConfig(form="cube", name="Jonas", grille="stars")
    assert export.bundle_zip(build(cfg)) == export.bundle_zip(build(cfg))


def test_bundle_contents_and_names() -> None:
    model = build(CaseConfig(form="radio", name="Jörg Ü"))
    assert export.file_stem(model.config) == "myboxi-radio-joerg-ue"
    zf = zipfile.ZipFile(
        io.BytesIO(export.bundle_zip(model, "https://app.myboxi.eu/gestalten?form=radio"))
    )
    names = zf.namelist()
    assert names[0] == "LIESMICH.txt"
    assert "myboxi-radio-joerg-ue-platte-1.3mf" in names
    assert "stl/myboxi-radio-joerg-ue-front-einlage.stl" in names
    config = json.loads(zf.read("konfiguration.json"))
    assert config["generator"] == GENERATOR_VERSION
    assert config["config"]["name"] == "Jörg Ü"
    readme = zf.read("LIESMICH.txt").decode()
    assert "Kopf 3" in readme
    assert "https://app.myboxi.eu/gestalten?form=radio" in readme


def test_preview_round_trip() -> None:
    model = build(CaseConfig(form="bear"))
    info, meshes = export.read_preview(export.preview(model))
    meshes_info: list[dict[str, object]] = info["meshes"]  # type: ignore[assignment]
    kinds = [str(m["kind"]) for m in meshes_info]
    assert kinds.count("component") == len(model.components)
    first_verts, first_tris = mesh_arrays(model.pieces[0].solid)
    verts, tris = meshes[0]
    assert np.allclose(verts, first_verts, atol=0.006)
    assert np.array_equal(tris, first_tris)


def test_render_png() -> None:
    png = render(assembled(build(CaseConfig(form="cube"))), size=(160, 120))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (160, 120)
