"""Speech for the box prompts with Kokoro and the voice Thorsten-Voice/Kokoro (build time only).

Reads {"name": "text", ...} as JSON on stdin and writes <out>/<name>.wav (24 kHz mono).
Called by build_prompts.py; needs the packages of requirements-tts.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from huggingface_hub import hf_hub_download
from kokoro import KModel, KPipeline

REPO = "Thorsten-Voice/Kokoro"
REVISION = "734e593d320a3d876bede7020f773dfd481a0cc7"  # default checkpoint: epoch 5
BASE = "hexgrad/Kokoro-82M"  # architecture reference; nothing is downloaded from it
SAMPLE_RATE = 24000


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    args = p.parse_args()
    texts: dict[str, str] = json.load(sys.stdin)

    def fetch(name: str) -> str:
        return hf_hub_download(REPO, name, revision=REVISION, cache_dir=args.cache)

    model = KModel(repo_id=BASE, config=fetch("config.json"), model=fetch("model.pth")).eval()
    voice = torch.load(fetch("voices/thorsten.pt"), map_location="cpu", weights_only=True)
    pipeline = KPipeline(lang_code="d", repo_id=BASE, model=model)
    g2p = pipeline.g2p

    def patched_g2p(text: str):
        # IPA short ü (U+028F) is not in Kokoro's vocabulary; the voice was trained with "y".
        phonemes, tokens = g2p(text)
        return phonemes.replace("\u028f", "y"), tokens

    pipeline.g2p = patched_g2p
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        chunks = [np.asarray(audio) for _, _, audio in pipeline(text, voice=voice, speed=1.0)]
        if not chunks:
            raise SystemExit(f"no audio for {name!r}")
        sf.write(args.out / f"{name}.wav", np.concatenate(chunks), SAMPLE_RATE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
