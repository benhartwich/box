"""myboxi-case: build print files, render previews and run the checks from the command line.

Options are given as key=value, the same keys as in the configurator URL, e.g.
    myboxi-case build form=bear name=Mia color_body=braun --out mia.zip
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

from pydantic import ValidationError

from myboxi_case.build import build
from myboxi_case.checks import check
from myboxi_case.config import CaseConfig
from myboxi_case.export import bundle_zip
from myboxi_case.layout import LayoutError
from myboxi_case.render import assembled, render


def _config(pairs: list[str]) -> CaseConfig:
    query: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {pair!r}")
        query[key] = value
    try:
        return CaseConfig.from_query(query)
    except ValidationError as exc:
        raise SystemExit(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="myboxi-case", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build", help="write the ZIP with 3MF, STL and instructions")
    p.add_argument("options", nargs="*")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("render", help="write a PNG preview")
    p.add_argument("options", nargs="*")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--inside", action="store_true", help="show the bought parts")
    p.add_argument("--explode", type=float, default=0.0)
    p.add_argument("--size", default="800x600")
    p = sub.add_parser("check", help="run the checks (all supported combinations with --all)")
    p.add_argument("options", nargs="*")
    p.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "check" and args.all:
        return _check_all()
    cfg = _config(args.options)
    try:
        model = build(cfg)
    except LayoutError as exc:
        print(exc, file=sys.stderr)
        return 2
    match args.command:
        case "build":
            args.out.write_bytes(bundle_zip(model))
        case "render":
            w, _, h = args.size.partition("x")
            items = assembled(model, inside=args.inside, explode=args.explode)
            args.out.write_bytes(render(items, size=(int(w), int(h))))
        case _:
            issues = check(model)
            for issue in issues:
                print(issue)
            return 1 if issues else 0
    return 0


def _check_all() -> int:
    failed = 0
    tried = 0
    for form, board, power, grille, speaker, button in itertools.product(
        ("radio", "cube", "bear"), ("zero2w", "pi4"), ("usbc", "powerbank"),
        ("dots", "stars", "hearts", "lines"), (40, 50, 57), (16, 24),
    ):  # fmt: skip
        cfg = CaseConfig.model_validate(
            {"form": form, "board": board, "power": power, "grille": grille,
             "speaker": speaker, "button": button, "name": "Großmama Ölçü"}
        )  # fmt: skip
        try:
            model = build(cfg)
        except LayoutError:
            continue
        tried += 1
        issues = check(model)
        if issues:
            failed += 1
            print(cfg.query(), *issues, sep="\n  ")
    print(f"{tried} combinations, {failed} with issues")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
