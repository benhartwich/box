"""Build the box prompts from agent/prompts.toml into Opus files (48 kHz mono).

    uv venv build/tts --python 3.13
    uv pip install -p build/tts --index-url https://download.pytorch.org/whl/cpu torch==2.14.0
    uv pip install -p build/tts -r tools/prompts/requirements-tts.txt
    build/tts/bin/python tools/prompts/build_prompts.py --out build/prompts

    uv run python tools/prompts/build_prompts.py --out build/prompts --tones-only

Speech: Kokoro with the voice Thorsten-Voice/Kokoro (tools/prompts/kokoro_tts.py), at build
time only; nothing of it runs on the box. Tones are generated with ffmpeg. NOTICE.txt next to
the prompts names the voice and its licence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "agent" / "prompts.toml"
TTS = Path(__file__).with_name("kokoro_tts.py")
# The voice must be free for any use. Not allowed: Piper voices finetuned from en_US-lessac
# (e.g. de_DE-thorsten), whose recordings are licensed for research only.
NOTICE = """\
Myboxi prompts: speech synthesized at build time with Kokoro (https://github.com/hexgrad/kokoro)
and the voice Thorsten-Voice/Kokoro (https://huggingface.co/Thorsten-Voice/Kokoro),
then loudness-normalized and encoded to Opus. Tones: generated with ffmpeg.

Voice model: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0).
Fine-tuned by Thorsten Mueller from hexgrad/Kokoro-82M (Apache License 2.0; training data
and attributions: https://huggingface.co/hexgrad/Kokoro-82M) on the Thorsten-Voice dataset
(CC0 1.0, https://www.thorsten-voice.de/).
"""
OPUS = ["-ar", "48000", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-map_metadata", "-1"]


def load(path: Path = PROMPTS) -> dict[str, dict[str, Any]]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def tone_graph(steps: list[list[int]]) -> str:
    parts: list[str] = []
    for i, (freq, ms) in enumerate(steps):
        d = ms / 1000
        if freq == 0:
            parts.append(f"anullsrc=r=48000:cl=mono,atrim=duration={d}[s{i}]")
        else:
            fade = min(0.02, d / 4)
            parts.append(
                f"sine=frequency={freq}:sample_rate=48000:duration={d},"
                f"afade=t=in:d={fade},afade=t=out:st={d - fade}:d={fade}[s{i}]"
            )
    inputs = "".join(f"[s{i}]" for i in range(len(steps)))
    return ";".join(parts) + f";{inputs}concat=n={len(steps)}:v=0:a=1,volume=0.5[out]"


def build_tone(steps: list[list[int]], out: Path) -> None:
    ffmpeg("-filter_complex", tone_graph(steps), "-map", "[out]", *OPUS, str(out))


def synthesize(texts: dict[str, str], cache: Path, out: Path) -> None:
    """Every text in one run (loading the model is the slow part): out/<name>.wav."""
    subprocess.run(
        [sys.executable, str(TTS), "--out", str(out), "--cache", str(cache)],
        input=json.dumps(texts),
        text=True,
        check=True,
    )


def build_speech(
    spec: dict[str, Any], prompts: dict[str, dict[str, Any]], wav: Path, out: Path
) -> None:
    speech = "[1:a]aresample=48000,aformat=channel_layouts=mono,loudnorm=I=-16:TP=-1.5[sp]"
    if "before" in spec:
        before = tone_graph(prompts[spec["before"]]["tone"]).replace("[out]", "[t]")
        graph = f"{before};{speech};[t][sp]concat=n=2:v=0:a=1[out]"
        ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-i", str(wav),
               "-filter_complex", graph, "-map", "[out]", *OPUS, str(out))  # fmt: skip
    else:
        ffmpeg("-i", str(wav), "-filter_complex", speech.replace("[1:a]", "[0:a]"),
               "-map", "[sp]", *OPUS, str(out))  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cache", type=Path, default=Path("build/voices"), help="model downloads")
    p.add_argument("--tones-only", action="store_true")
    args = p.parse_args(argv)
    prompts = load()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        speech = Path(tmp)
        if not args.tones_only:
            texts = {name: str(spec["text"]) for name, spec in prompts.items() if "text" in spec}
            synthesize(texts, args.cache, speech)
        for name, spec in prompts.items():
            target = args.out / f"{name}.opus"
            if "tone" in spec:
                build_tone(spec["tone"], target)
            elif not args.tones_only:
                build_speech(spec, prompts, speech / f"{name}.wav", target)
            print(f"  {name}")
    if not args.tones_only:
        (args.out / "NOTICE.txt").write_text(NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
