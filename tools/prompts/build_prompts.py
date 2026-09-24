"""Build the box prompts from agent/prompts.toml into Opus files (48 kHz mono).

    uv run --with piper-tts python tools/prompts/build_prompts.py --out build/prompts
    uv run python tools/prompts/build_prompts.py --out build/prompts --tones-only

Speech uses Piper (https://github.com/OHF-voice/piper1-gpl) at build time only; nothing of
Piper runs on the box. Tones are generated with ffmpeg.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "agent" / "prompts.toml"
DEFAULT_VOICE = "de_DE-thorsten-medium"
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


def synthesize(text: str, voice: str, voices_dir: Path, wav: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "piper", "--data-dir", str(voices_dir), "-m", voice,
         "-f", str(wav), "--", text],
        check=True,
    )  # fmt: skip


def build_speech(
    spec: dict[str, Any],
    prompts: dict[str, dict[str, Any]],
    voice: str,
    voices_dir: Path,
    out: Path,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "speech.wav"
        synthesize(str(spec["text"]), voice, voices_dir, wav)
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
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--voices-dir", type=Path, default=Path("build/voices"))
    p.add_argument("--tones-only", action="store_true")
    args = p.parse_args(argv)
    prompts = load()
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.tones_only:
        args.voices_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "piper.download_voices",
                "--data-dir",
                str(args.voices_dir),
                args.voice,
            ],
            check=True,
        )
    for name, spec in prompts.items():
        target = args.out / f"{name}.opus"
        if "tone" in spec:
            build_tone(spec["tone"], target)
        elif not args.tones_only:
            build_speech(spec, prompts, args.voice, args.voices_dir, target)
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
