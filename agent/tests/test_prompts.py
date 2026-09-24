"""Every prompt the agent can play exists in agent/prompts.toml (SPEC §1.7)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from myboxi_agent.adapters.mpv import KNOWN_PROMPTS

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "prompts" / "build_prompts.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_prompts", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_prompts"] = module
    spec.loader.exec_module(module)
    return module


def test_all_prompts_are_defined() -> None:
    prompts = _module().load()
    assert set(KNOWN_PROMPTS) <= set(prompts)
    for name, spec in prompts.items():
        assert ("tone" in spec) != ("text" in spec), name
        if "before" in spec:
            assert "tone" in prompts[spec["before"]], name


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_tones_are_built(tmp_path: Path) -> None:
    assert _module().main(["--out", str(tmp_path), "--tones-only"]) == 0
    for tone in ("tone_start", "tone_error", "tone_attention", "tone_loading"):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "csv=p=0", str(tmp_path / f"{tone}.opus")],
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip
        assert out == "opus,48000,1"


def test_voice_licence_is_shipped() -> None:
    module = _module()
    # Piper voices finetuned from en_US-lessac are research-only (see build_prompts.NOTICE).
    assert "Apache License 2.0" in module.NOTICE
    assert "CC0" in module.NOTICE
    assert "lessac" not in module.NOTICE
    assert module.TTS.is_file()
