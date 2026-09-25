"""Files for download and preview: STL, 3MF (with tool heads for Orca), preview mesh, ZIP bundle.

Everything is deterministic: the same configuration gives byte-identical files.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

import numpy as np
import numpy.typing as npt
from manifold3d import Manifold

from myboxi_case import GENERATOR_VERSION
from myboxi_case.build import CaseModel, Piece
from myboxi_case.config import CaseConfig
from myboxi_case.geom import bbox, mesh_arrays

PLATE = (5.0, 265.0)  # usable area on a Snapmaker U1 plate (270 x 270), x and y
# Kept free for the U1's prime tower (Orca default position 15/220, 30 mm wide, plus brim).
PRIME_TOWER = (0.0, 190.0, 72.0, 270.0)
GAP = 6.0
ZIP_DATE = (2026, 1, 1, 0, 0, 0)
PREVIEW_MAGIC = b"MBXP"
PREVIEW_STEP = 0.01  # mm per int16 unit


def stl(m: Manifold, name: str = "myboxi") -> bytes:
    verts, tris = mesh_arrays(m)
    corners = verts[tris]  # (n, 3, 3)
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = (normals / np.maximum(lengths, 1e-12)).astype(np.float32)
    record = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    record["n"] = normals
    record["v"] = corners
    header = name.encode("ascii", "replace")[:80].ljust(80, b" ")
    return header + struct.pack("<I", len(tris)) + record.tobytes()


@dataclass(frozen=True)
class Placed:
    piece: Piece
    solid: Manifold  # print orientation, placed on its plate
    inlay: Manifold
    plate: int


Rect = tuple[float, float, float, float]


def _free(r: Rect, taken: list[Rect]) -> bool:
    x0, y0, x1, y1 = r
    lo, hi = PLATE
    if x0 < lo or y0 < lo or x1 > hi or y1 > hi:
        return False
    for tx0, ty0, tx1, ty1 in [*taken, PRIME_TOWER]:
        if x0 < tx1 + GAP and tx0 < x1 + GAP and y0 < ty1 + GAP and ty0 < y1 + GAP:
            return False
    return True


def _spot(w: float, d: float, taken: list[Rect]) -> tuple[float, float] | None:
    lo = PLATE[0]
    xs = {lo, *(r[2] + GAP for r in taken)}
    ys = {lo, *(r[3] + GAP for r in taken)}
    for y in sorted(ys):
        for x in sorted(xs):
            if _free((x, y, x + w, y + d), taken):
                return (x, y)
    return None


def arrange(pieces: Iterable[Piece]) -> list[Placed]:
    """Print-oriented pieces on U1 plates: bottom-left packing, largest first, turned by 90° when
    that fits better. Pieces that do not fit go to the next plate."""
    items: list[tuple[Piece, Manifold, Manifold]] = []
    for p in pieces:
        inlay = p.printed(p.inlay) if not p.inlay.is_empty() else Manifold()
        items.append((p, p.printed(p.solid), inlay))

    def area(m: Manifold) -> float:
        x0, y0, _, x1, y1, _ = bbox(m)
        return (x1 - x0) * (y1 - y0)

    items.sort(key=lambda it: -area(it[1]))
    plates: list[list[Rect]] = [[]]
    placed: list[Placed] = []
    for p, solid, inlay in items:
        best: tuple[int, float, float, int] | None = None  # plate, y, x, turn
        for plate, taken in enumerate(plates):
            for turn in (0, 90):
                x0, y0, _, x1, y1, _ = bbox(solid.rotate((0, 0, turn)))
                spot = _spot(x1 - x0, y1 - y0, taken)
                if spot is not None and (best is None or (plate, spot[1], spot[0]) < best[:3]):
                    best = (plate, spot[1], spot[0], turn)
            if best is not None:
                break
        if best is None:
            plates.append([])
            x0, y0, _, x1, y1, _ = bbox(solid)
            best = (len(plates) - 1, PLATE[0], PLATE[0], 0)
        plate, y, x, turn = best
        turned = solid.rotate((0, 0, turn))
        bx0, by0, _, bx1, by1, _ = bbox(turned)
        move = (x - bx0, y - by0, 0.0)
        plates[plate].append((x, y, x + bx1 - bx0, y + by1 - by0))
        moved_inlay = inlay.rotate((0, 0, turn)).translate(move) if not inlay.is_empty() else inlay
        placed.append(Placed(p, turned.translate(move), moved_inlay, plate))
    return placed


def _mesh_xml(m: Manifold) -> str:
    verts, tris = mesh_arrays(m)
    v = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in verts.tolist())
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris.tolist())
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def threemf(model: CaseModel) -> list[bytes]:
    """One 3MF per print plate; each object is a printed part, inlays are extra parts of it.

    Colours as 3MF base materials; tool heads in Metadata/model_settings.config, which Orca,
    Snapmaker Orca and Bambu Studio read to assign each part to a filament.
    """
    placed = arrange(model.pieces)
    plates = max(p.plate for p in placed) + 1
    return [
        _threemf_plate(model, [p for p in placed if p.plate == plate], plate, plates)
        for plate in range(plates)
    ]


def _threemf_plate(model: CaseModel, placed: list[Placed], plate: int, plates: int) -> bytes:
    colors: list[str] = []

    def material(color: str) -> int:
        if color not in colors:
            colors.append(color)
        return colors.index(color)

    objects: list[str] = []
    builds: list[str] = []
    config: list[str] = []
    next_id = 2  # id 1: base materials
    for item in placed:
        p = item.piece
        members = [(p.label, item.solid, p.color, p.tool)]
        if not item.inlay.is_empty():
            members.append((f"{p.label} Einlage", item.inlay, p.inlay_color, p.inlay_tool))
        mesh_ids: list[int] = []
        for label, solid, color, _tool in members:
            objects.append(
                f'<object id="{next_id}" type="model" name={quoteattr(label)} pid="1" '
                f'pindex="{material(color)}">{_mesh_xml(solid)}</object>'
            )
            mesh_ids.append(next_id)
            next_id += 1
        comps = "".join(f'<component objectid="{i}"/>' for i in mesh_ids)
        objects.append(
            f'<object id="{next_id}" type="model" name={quoteattr(p.label)}>'
            f"<components>{comps}</components></object>"
        )
        builds.append(f'<item objectid="{next_id}"/>')
        parts = "".join(
            f'<part id="{mid}" subtype="normal_part">'
            f'<metadata key="name" value={quoteattr(label)}/>'
            f'<metadata key="extruder" value="{tool}"/></part>'
            for mid, (label, _s, _c, tool) in zip(mesh_ids, members, strict=True)
        )
        config.append(
            f'<object id="{next_id}"><metadata key="name" value={quoteattr(p.label)}/>'
            f'<metadata key="extruder" value="{p.tool}"/>{parts}</object>'
        )
        next_id += 1

    bases = "".join(f'<base name="{escape(c)}" displaycolor="{c}FF"/>' for c in colors)
    name = title(model.config) + (f" – Platte {plate + 1} von {plates}" if plates > 1 else "")
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="de-DE" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<metadata name="Title">{escape(name)}</metadata>'
        '<metadata name="Designer">Myboxi (myboxi.eu)</metadata>'
        '<metadata name="LicenseTerms">CC BY-SA 4.0</metadata>'
        f'<resources><basematerials id="1">{bases}</basematerials>{"".join(objects)}</resources>'
        f"<build>{''.join(builds)}</build></model>"
    )
    settings = f'<?xml version="1.0" encoding="UTF-8"?>\n<config>{"".join(config)}</config>'
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '<Default Extension="config" ContentType="text/xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    return _zip(
        [
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", rels),
            ("3D/3dmodel.model", model_xml),
            ("Metadata/model_settings.config", settings),
        ]
    )


def _zip(entries: Iterable[tuple[str, str | bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data.encode() if isinstance(data, str) else data)
    return buf.getvalue()


def title(cfg: CaseConfig) -> str:
    forms = {"radio": "Radio", "cube": "Würfel", "bear": "Bär"}
    name = f" „{cfg.name}“" if cfg.name else ""
    return f"Myboxi {forms[cfg.form]}{name}"


def file_stem(cfg: CaseConfig) -> str:
    """ASCII-safe file name stem, e.g. ``myboxi-radio-lotta``."""
    table = str.maketrans(
        {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}
    )
    name = cfg.name.translate(table).lower()
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return "-".join(part for part in ("myboxi", cfg.form, slug) if part)


def preview(model: CaseModel, *, components: bool = True) -> bytes:
    """Compact mesh for the web preview (assembly frame).

    Layout: b"MBXP", u32 header length, JSON header, then per mesh int16 positions (x, y, z in
    0.01 mm) and u32 triangle indices, each block padded to 4 bytes.
    """
    meshes: list[tuple[dict[str, object], Manifold]] = []
    for p in model.pieces:
        meshes.append(
            (
                {
                    "key": p.key,
                    "label": p.label,
                    "kind": "part",
                    "color": p.color,
                    "explode": p.explode,
                },
                p.solid,
            )
        )
        if not p.inlay.is_empty():
            meshes.append(
                (
                    {
                        "key": f"{p.key}_inlay",
                        "label": p.label,
                        "kind": "inlay",
                        "color": p.inlay_color,
                        "explode": p.explode,
                    },
                    p.inlay,
                )
            )
    if components:
        for c in model.components:
            meshes.append(
                (
                    {
                        "key": c.key,
                        "label": c.label,
                        "kind": "component",
                        "color": c.color,
                        "explode": (0, 0, 0),
                    },
                    c.solid,
                )
            )
    blobs: list[bytes] = []
    header: list[dict[str, object]] = []
    for meta, solid in meshes:
        verts, tris = mesh_arrays(solid)
        q = np.round(verts / PREVIEW_STEP).astype("<i2")
        pos = q.tobytes()
        pos += b"\0" * (-len(pos) % 4)
        idx = tris.astype("<u4").tobytes()
        header.append(meta | {"vertices": len(verts), "triangles": len(tris)})
        blobs += [pos, idx]
    info = {
        "version": GENERATOR_VERSION,
        "step": PREVIEW_STEP,
        "size": [model.layout.width, model.layout.depth, model.layout.height],
        "meshes": header,
    }
    head = json.dumps(info, separators=(",", ":"), ensure_ascii=False).encode()
    head += b" " * (-len(head) % 4)
    return PREVIEW_MAGIC + struct.pack("<I", len(head)) + head + b"".join(blobs)


def read_preview(
    data: bytes,
) -> tuple[dict[str, object], list[tuple[npt.NDArray[np.float32], npt.NDArray[np.uint32]]]]:
    """Decoder for tests (the browser has its own in case.js)."""
    if data[:4] != PREVIEW_MAGIC:
        raise ValueError("not a preview")
    (n,) = struct.unpack("<I", data[4:8])
    info = json.loads(data[8 : 8 + n])
    offset = 8 + n
    out: list[tuple[npt.NDArray[np.float32], npt.NDArray[np.uint32]]] = []
    for mesh in info["meshes"]:
        nv, nt = int(mesh["vertices"]), int(mesh["triangles"])
        pos = np.frombuffer(data, dtype="<i2", count=nv * 3, offset=offset).reshape(-1, 3)
        offset += nv * 6 + (-(nv * 6) % 4)
        idx = np.frombuffer(data, dtype="<u4", count=nt * 3, offset=offset).reshape(-1, 3)
        offset += nt * 12
        out.append(((pos * info["step"]).astype(np.float32), idx.astype(np.uint32)))
    return info, out


BOM: dict[str, str] = {
    "zero2w": "Raspberry Pi Zero 2 W mit angelöteter Stiftleiste",
    "pi4": "Raspberry Pi 4 (2 GB reichen)",
}


def _readme(model: CaseModel, files: list[str], url: str | None) -> str:
    cfg = model.config
    multi = cfg.colors == "multi"
    labels = {p.key: p.label for p in model.pieces}
    lines = [
        title(model.config),
        "=" * len(title(model.config)),
        "",
        "Druckvorlage für die Myboxi, erzeugt mit „Box gestalten“" + (f":\n{url}" if url else "."),
        "",
        "DATEIEN",
        *[f"  {name}" for name in files],
        "",
        "DRUCKEN",
        "  Material: PETG (robust, verträgt die Wärme des Pi). PLA geht für Pi Zero 2 W auch.",
        "  Schicht 0,2 mm, 4 Wände, 15 % Füllung, ohne Stützmaterial.",
        "  Alle Teile liegen schon richtig auf der Platte: den Korpus mit der Oberseite nach",
        "  unten, die Front mit der Sichtseite nach unten.",
        "  Figurensockel: Druckpause bei 2,2 mm, NFC-Tag (25 mm, NTAG213) einlegen, weiter.",
    ]
    if multi:
        lines += [
            "",
            "MEHRFARBIG (z. B. Snapmaker U1, Orca/Snapmaker Orca)",
            "  Die 3MF-Datei ordnet die Teile den Köpfen zu:",
            f"    Kopf 1: {labels['body']}, {labels['base']}",
            f"    Kopf 2: {labels['front']}, {labels['speaker_ring']}",
            "    Kopf 3: Einlagen (Name, Symbole, Figurenring) und Figurensockel",
            "  Ohne Mehrfarbdrucker: Einlagen-Teile löschen, die Gravur bleibt sichtbar.",
        ]
    lines += [
        "",
        "STÜCKLISTE",
        f"  {BOM[cfg.board]}",
        "  NFC-Modul PN532 V3 (I2C), NFC-Tags NTAG213/215",
        "  I2S-Verstärker MAX98357A",
        f"  Lautsprecher {cfg.speaker} mm, 3 W, 4 Ohm",
        f"  4 Taster {cfg.button} mm (Einbau, schließend)",
        "  USB-C-Einbaubuchse mit Kabel zum Pi"
        + (" und Powerbank (10 000 mAh, 93 x 61 x 23 mm)" if cfg.power == "powerbank" else ""),
        "  4 Schrauben M3 x 10 (Boden), 3 Schrauben M2,5 x 6 (Lautsprecherring),",
        "  4 Schrauben M2,5 x 6 (Pi)"
        + ("; Gewindeeinsätze M3 und M2,5" if cfg.fastening == "insert" else ", selbstschneidend"),
        "",
        "ZUSAMMENBAU (ohne Kleber)",
        "  1. Lautsprecher in die Front legen, Lautsprecherring aufschrauben.",
        "  2. NFC-Modul von unten in den Rahmen unter der Figurenmarke drücken (rastet ein).",
        "  3. Taster oben einsetzen und verschrauben, USB-C-Buchse hinten einschrauben.",
        "  4. Pi auf den Boden schrauben. Verstärker von oben in seinen Halter schieben:",
        "     die Platine klemmt in den Schlitzen.",
        "  5. Verkabeln (Pins: https://github.com/benhartwich/myboxi/blob/main/docs/hardware.md)",
        "     und die Kabel mit kleinen Kabelbindern an den Haltern auf dem Boden bündeln,",
        "     damit beim Schließen nichts eingeklemmt wird."
        + (
            "\n     Powerbank mit einem Klettband durch die Schlitze festzurren."
            if cfg.power == "powerbank"
            else ""
        ),
        "  6. Front von unten in die Schienen hinter dem Fenster schieben.",
        "  7. Boden einsetzen und mit 4 Schrauben M3 festschrauben.",
        "",
        'Ohne Löten: Pi Zero 2 W mit vorgelöteter Stiftleiste ("WH") kaufen, Taster mit',
        "Anschlusslitzen, Dupont-Kabel Buchse/Buchse 20 cm. Beim PN532 liegt die Stiftleiste",
        "oft lose bei; es gibt Module mit fertig eingelöteter Leiste.",
        "",
        "Maße vor dem Druck mit den eigenen Teilen vergleichen: Taster, USB-C-Buchse und",
        "Lautsprecher unterscheiden sich je nach Händler. Anleitung und Maße:",
        "https://github.com/benhartwich/myboxi/blob/main/docs/gehaeuse.md",
        "",
        "LIZENZ",
        "  Druckdateien: CC BY-SA 4.0 (Namensnennung: Myboxi, myboxi.eu).",
        "  Generator: GPL-3.0-or-later, https://github.com/benhartwich/myboxi",
        "",
        f"Generator {GENERATOR_VERSION} · Konfiguration {cfg.digest()[:12]}",
    ]
    return "\n".join(lines) + "\n"


def bundle_zip(model: CaseModel, url: str | None = None) -> bytes:
    """Everything for printing: 3MF per plate, STL per part, configuration and a German readme."""
    stem = file_stem(model.config)
    entries: list[tuple[str, str | bytes]] = []
    plates = threemf(model)
    for i, data in enumerate(plates):
        name = f"{stem}.3mf" if len(plates) == 1 else f"{stem}-platte-{i + 1}.3mf"
        entries.append((name, data))
    for p in model.pieces:
        entries.append((f"stl/{stem}-{p.key}.stl", stl(p.printed(p.solid), p.label)))
        if not p.inlay.is_empty():
            entries.append((f"stl/{stem}-{p.key}-einlage.stl", stl(p.printed(p.inlay), p.label)))
    config = {
        "generator": GENERATOR_VERSION,
        "digest": model.config.digest(),
        "config": model.config.model_dump(mode="json"),
    }
    if url:
        config["url"] = url
    entries.append(("konfiguration.json", json.dumps(config, indent=2, ensure_ascii=False) + "\n"))
    files = [name for name, _ in entries]
    entries.insert(0, ("LIESMICH.txt", _readme(model, files, url)))
    return _zip(entries)
