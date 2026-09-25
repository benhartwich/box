"""Static checks of the files the box image ships (image/files)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from myboxi_agent.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "image" / "files"
ENV = FILES / "etc" / "myboxi-agent" / "myboxi-agent.env"
UNITS = sorted(FILES.glob("**/*.service"))


def _required_environment_files(unit: Path) -> list[str]:
    paths: list[str] = []
    for line in unit.read_text().splitlines():
        if line.startswith("EnvironmentFile="):
            path = line.removeprefix("EnvironmentFile=")
            if not path.startswith("-"):
                paths.append(path)
    return paths


def test_units_exist() -> None:
    assert {u.name for u in UNITS} >= {
        "myboxi-agent.service",
        "myboxi-setupd.service",
        "myboxi-firstboot.service",
    }


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.name)
def test_required_environment_files_are_shipped(unit: Path) -> None:
    # A missing EnvironmentFile= stops the unit from starting at all.
    for path in _required_environment_files(unit):
        assert (FILES / path.lstrip("/")).is_file(), f"{unit.name}: {path} not in image/files"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_image_files_are_not_git_ignored() -> None:
    # Ignored files exist locally but are missing in the CI build.
    files = [str(p.relative_to(ROOT)) for p in FILES.rglob("*") if p.is_file()]
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--no-index", *files],
        capture_output=True,
        text=True,
    )
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.stdout == ""


def test_env_file_sets_known_settings() -> None:
    fields = set(Settings.model_fields)
    values: dict[str, str] = {}
    for line in ENV.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert key.startswith("MYBOXI_AGENT_"), key
        name = key.removeprefix("MYBOXI_AGENT_").lower()
        assert name in fields, key
        values[name] = value
    settings = Settings.model_validate(values)
    assert settings.default_server_url is not None
    assert str(settings.default_server_url).startswith("https://")


def test_services_run_the_current_release() -> None:
    """SPEC v0.7 §11.1: the updater switches /opt/myboxi-agent/current atomically."""
    for unit in UNITS:
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart=/opt/myboxi-agent"):
                assert line.startswith("ExecStart=/opt/myboxi-agent/current/.venv/bin/"), unit
    assert "/opt/myboxi-agent/current/prompts" in ENV.read_text()


def test_updater_units_keys_and_permissions() -> None:
    system = FILES / "etc" / "systemd" / "system"
    assert "myboxi-agent update" in (system / "myboxi-updater.service").read_text()
    assert "OnUnitActiveSec=1h" in (system / "myboxi-updater.timer").read_text()
    rule = (FILES / "etc" / "polkit-1" / "rules.d" / "50-myboxi-setupd.rules").read_text()
    assert '"myboxi-updater.service" && verb == "start"' in rule
    keys = sorted((FILES / "etc" / "myboxi-agent" / "update-keys").glob("*.pem"))
    assert [k.name for k in keys] == ["myboxi-2026-backup.pem", "myboxi-2026-main.pem"]
    for key in keys:
        text = key.read_text()
        assert text.startswith("-----BEGIN PUBLIC KEY-----")
        assert "PRIVATE" not in text
    apt = FILES / "etc" / "apt" / "apt.conf.d" / "52myboxi-unattended-upgrades"
    assert 'Automatic-Reboot "false"' in apt.read_text()


def test_unit_commands_exist() -> None:
    """Every ``myboxi-agent`` command a unit runs is known to the CLI."""
    from myboxi_agent.cli import build_parser

    parser = build_parser()
    for unit in UNITS:
        for line in unit.read_text().splitlines():
            key, _, command = line.partition("=")
            if key in ("ExecStart", "ExecStopPost") and "/bin/myboxi-agent " in command:
                args = command.split("/bin/myboxi-agent ", 1)[1].split()
                parser.parse_args(args)  # raises SystemExit if unknown


def test_no_soloist_binary_in_the_image() -> None:
    """CLAUDE.md: Soloist is never shipped, the box downloads it (SPEC v0.9 §8.1)."""
    for path in FILES.rglob("*"):
        if path.is_file():
            assert not path.name.startswith("soloist_release"), path
            assert path.read_bytes()[:4] != b"\x7fELF", path


def test_firewall_only_lets_the_home_network_in() -> None:
    """SPEC v0.9 §10: Spotify Connect is the only LAN exception; nothing from the internet."""
    rules = (FILES / "etc" / "nftables.conf").read_text()
    assert "policy drop;" in rules
    assert "192.168.0.0/16" in rules
    assert "fc00::/7" in rules
    if shutil.which("nft") and os.geteuid() == 0:  # nft -c needs netlink, even to only check
        subprocess.run(["nft", "-c", "-f", str(FILES / "etc" / "nftables.conf")], check=True)


def test_soloist_websocket_default_port_is_loopback_only() -> None:
    from myboxi_agent.soloist.install import SoloistPaths
    from myboxi_agent.soloist.runner import soloist_argv

    argv = soloist_argv(
        SoloistPaths(Path("/x")), key="k" * 16, name="n", port=Settings().soloist_ws_port
    )
    assert argv[argv.index("--ws") + 1].startswith("127.0.0.1:")
