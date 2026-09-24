"""Acceptance against the deployed container, through nginx and TLS.

Signs in, creates a collection, uploads an MP3 (transcoded by the worker unit), binds a
figure and then runs tools/fake-device end to end: pairing, state, asset download via
X-Accel-Redirect (SHA-256, Range, ETag), events, unpair.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import httpx

ROOT = Path(__file__).resolve().parents[2]
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def load_fake_device() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fake_device", ROOT / "tools/fake-device/fake_device.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_device"] = module
    spec.loader.exec_module(module)
    return module


def csrf(html: str) -> str:
    m = CSRF.search(html)
    assert m, "no csrf token"
    return m.group(1)


def test_mp3() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "Gute Nacht.mp3"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=5", "-c:a", "libmp3lame", str(mp3)],
            check=True,
        )  # fmt: skip
        return mp3.read_bytes()


async def main(base_url: str, email: str, password: str, audio: bytes) -> None:
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60) as web:  # noqa: S501
        r = await web.get("/login")
        assert r.status_code == 200, r.status_code
        assert r.headers["strict-transport-security"]
        r = await web.post(
            "/login", data={"email": email, "password": password, "csrf_token": csrf(r.text)}
        )
        assert r.status_code == 303, r.status_code
        home = await web.get("/", follow_redirects=True)
        tid = str(home.url).rstrip("/").rsplit("/", 1)[1]
        token = csrf(home.text)
        print(f"  ✓ Login über nginx/TLS, Mandant {tid}")

        r = await web.post(
            f"/t/{tid}/contents",
            data={"kind": "collection", "title": "Abendlieder", "csrf_token": token},
        )
        cid = r.headers["location"].rsplit("/", 1)[1]
        r = await web.post(
            f"/t/{tid}/contents/{cid}/uploads",
            data={"csrf_token": token, "profile": "music"},
            files={"files": ("Gute Nacht.mp3", audio, "audio/mpeg")},
        )
        assert r.status_code == 303, r.text
        for _ in range(60):
            tracks = (await web.get(f"/t/{tid}/contents/{cid}/tracks")).text
            if "Gute Nacht" in tracks and "every 2s" not in tracks:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("upload was not processed by the worker")
        assert "fehlgeschlagen" not in tracks
        print("  ✓ Upload von der Worker-Unit transkodiert")

        r = await web.post(
            f"/t/{tid}/figures", data={"uid": "04A2B3C4D5E680", "label": "Bär", "csrf_token": token}
        )
        fid = r.headers["location"].rsplit("/", 1)[1]
        r = await web.post(
            f"/t/{tid}/figures/{fid}/binding",
            data={"content_id": cid, "resume": "true", "repeat": "off", "csrf_token": token},
        )
        assert r.status_code == 200

        r = await web.get("/_assets/00/00/" + "0" * 64 + ".opus")
        assert r.status_code == 404, "internal location must not be reachable from outside"
        print("  ✓ /_assets/ ist von außen nicht erreichbar")

    fake = load_fake_device()
    claim = await fake.user_claim(
        base_url, email=email, password=password, tenant_id=tid, name="Container-Box", verify=False
    )
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=60) as client:  # noqa: S501
        report = await fake.run(client, claim=claim, poll_interval=3.0, expect_assets=1)
        sha = next(iter(report.assets))
    # Headers as delivered by nginx after X-Accel-Redirect.
    assert report.asset_headers["content-type"] == "audio/ogg; codecs=opus", report.asset_headers
    assert "immutable" in report.asset_headers["cache-control"], report.asset_headers
    print(f"  ✓ Fake-Device komplett, Asset {sha[:12]}… über X-Accel-Redirect geladen")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], test_mp3()))
