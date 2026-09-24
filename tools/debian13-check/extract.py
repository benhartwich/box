"""Turn the bash blocks of docs/betrieb-debian13.md into one script.

A block preceded by ``<!-- check: replace=NAME -->`` is replaced by ``replace/NAME.sh``,
``<!-- check: skip -->`` skips it. Everything else runs exactly as documented.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCK = re.compile(
    r"(?:<!-- check: (?P<directive>[^>]*?) -->\s*\n)?```bash\n(?P<body>.*?)```", re.S
)


def main(doc: Path, replacements: Path) -> str:
    parts = ["set -euxo pipefail"]
    for m in BLOCK.finditer(doc.read_text()):
        directive = (m.group("directive") or "").strip()
        if directive == "skip":
            continue
        if directive.startswith("replace="):
            name = directive.split("=", 1)[1]
            parts.append(f"# --- replaced: {name}\n" + (replacements / f"{name}.sh").read_text())
            continue
        parts.append("# --- from the documentation\n" + m.group("body"))
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    sys.stdout.write(main(Path(sys.argv[1]), Path(sys.argv[2])))
