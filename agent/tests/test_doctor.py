"""``myboxi-agent doctor --offline`` as run during the image build."""

from __future__ import annotations

from pathlib import Path

from myboxi_agent.adapters.mpv import KNOWN_PROMPTS
from myboxi_agent.config import Settings
from myboxi_agent.doctor import check_prompts, run_doctor


def test_prompts_check_reports_missing_and_accepts_own_recordings(tmp_path: Path) -> None:
    generated = tmp_path / "prompts"
    generated.mkdir()
    settings = Settings(data_dir=tmp_path / "data", prompts_dir=generated)
    assert check_prompts(settings).level == "fail"
    for p in KNOWN_PROMPTS[1:]:
        (generated / f"{p}.opus").write_bytes(b"x")
    own = settings.custom_prompts_dir
    own.mkdir(parents=True)
    (own / f"{KNOWN_PROMPTS[0]}.opus").write_bytes(b"x")
    assert check_prompts(settings).level == "ok"


def test_offline_doctor_output(tmp_path: Path) -> None:
    lines: list[str] = []
    settings = Settings(data_dir=tmp_path, prompts_dir=tmp_path / "none", mpv_path="true")
    code = run_doctor(settings, offline=True, echo=lines.append)
    assert code == 1  # prompts and hardware modules are missing here
    assert any(line.startswith("✓ database") for line in lines)
    assert any(line.startswith("✗ prompts") for line in lines)
