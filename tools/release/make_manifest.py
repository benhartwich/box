"""Write the update channel manifest for a release (SPEC v0.7 §11.1).

    python tools/release/make_manifest.py --version 0.3.0 \
        --bundle build/myboxi-agent-0.3.0-arm64.tar.xz \
        --url https://github.com/…/releases/download/box-v0.3.0/myboxi-agent-0.3.0-arm64.tar.xz \
        --out build/manifest.json

The CI signs the file afterwards (openssl pkeyutl -sign -rawin, Ed25519); only standard
library here so it runs anywhere.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def manifest(version: str, bundle: Path, url: str, now: dt.datetime | None = None) -> bytes:
    if not VERSION.match(version):
        raise SystemExit(f"not a release version: {version!r}")
    if not url.startswith("https://"):
        raise SystemExit("the bundle URL must be https")
    digest = hashlib.sha256()
    with bundle.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    released = (now or dt.datetime.now(dt.UTC)).replace(microsecond=0)
    data = {
        "channel": "stable",
        "version": version,
        "released_at": released.isoformat().replace("+00:00", "Z"),
        "bundle": {"url": url, "sha256": digest.hexdigest(), "size": bundle.stat().st_size},
    }
    return (json.dumps(data, indent=2) + "\n").encode()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True)
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    args.out.write_bytes(manifest(args.version, args.bundle, args.url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
