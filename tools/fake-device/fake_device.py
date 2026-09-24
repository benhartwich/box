"""Fake box: runs the complete device flow against a server (SPEC §7).

    uv run python tools/fake-device/fake_device.py --base-url https://box.example.org \
        --auto-claim --email owner@example.org --tenant <tenant-id>   # password from $BOX_PASSWORD

Steps: pairing/start -> claim (by a user, or automatically) -> poll -> token -> reported ->
state -> download every asset and verify SHA-256 (plus Range and ETag checks) -> event ->
duplicate event -> unpair -> token must fail. Every response is validated against
``box_protocol``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from box_protocol.auth import DeviceTokenRequest, DeviceTokenResponse
from box_protocol.errors import ErrorCode, ErrorResponse
from box_protocol.events import EventBatchRequest, EventBatchResponse, TokenUnknownEvent
from box_protocol.pairing import (
    ClaimRequest,
    ClaimResponse,
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from box_protocol.reported import ReportedMessage
from box_protocol.state import StateResponse

API = "/api/v1"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class FlowError(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise FlowError(message)


def ulid() -> str:
    """ULID: 48-bit milliseconds + 80 random bits, Crockford Base32."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


@dataclass
class Report:
    device_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    config_rev: int | None = None
    assets: dict[str, int] = field(default_factory=dict[str, int])
    asset_headers: dict[str, str] = field(default_factory=dict[str, str])
    steps: list[str] = field(default_factory=list[str])

    def step(self, text: str) -> None:
        self.steps.append(text)
        print(f"  ✓ {text}", flush=True)


ClaimFn = Callable[[str], Awaitable[None]]


async def user_claim(
    base_url: str,
    *,
    email: str,
    password: str,
    tenant_id: str,
    name: str,
    verify: bool | str = True,
) -> ClaimFn:
    """Return a claim function acting as a signed-in admin (session cookie + CSRF header)."""

    async def claim(code: str) -> None:
        async with httpx.AsyncClient(base_url=base_url, verify=verify, timeout=30) as web:
            login_page = await web.get("/login")
            token = _csrf(login_page.text)
            r = await web.post(
                "/login", data={"email": email, "password": password, "csrf_token": token}
            )
            check(r.status_code == 303, f"login failed: {r.status_code}")
            page = await web.get(f"/t/{tenant_id}/members")
            check(page.status_code == 200, f"tenant page: {page.status_code}")
            r = await web.post(
                f"{API}/tenants/{tenant_id}/devices/claim",
                json=ClaimRequest(code=code, name=name).model_dump(),
                headers={"X-CSRF-Token": _csrf(page.text)},
            )
            check(r.status_code == 200, f"claim failed: {r.status_code} {r.text}")
            ClaimResponse.model_validate_json(r.content)

    return claim


def _csrf(html: str) -> str:
    m = _CSRF_RE.search(html)
    check(m is not None, "no CSRF token found")
    assert m is not None
    return m.group(1)


async def prompt_claim(code: str) -> None:
    spoken = " - ".join(code)
    print(f"\n  Kopplungscode: {code}   (angesagt: {spoken})")
    print("  Bitte in der Web-UI unter Boxen → Box hinzufügen eingeben.\n", flush=True)


async def run(
    client: httpx.AsyncClient,
    *,
    claim: ClaimFn,
    device_id: uuid.UUID | None = None,
    poll_interval: float = 3.0,
    poll_timeout: float = 600.0,
    expect_assets: int | None = None,
) -> Report:
    report = Report(device_id or uuid.uuid4())

    # 1. pairing/start
    r = await client.post(
        f"{API}/pairing/start",
        json=PairingStartRequest(
            device_id=report.device_id, hw_model="fake-device", agent_version="0.0.0-fake"
        ).model_dump(mode="json"),
    )
    check(r.status_code == 200, f"pairing/start: {r.status_code} {r.text}")
    started = PairingStartResponse.model_validate_json(r.content)
    report.step(f"pairing/start → Code {started.code}, gültig {started.expires_in}s")

    # 2. poll before the claim → 202 pending
    r = await client.get(f"{API}/pairing/poll", params={"poll_token": started.poll_token})
    check(r.status_code == 202, f"poll before claim: {r.status_code}")
    PairingPending.model_validate_json(r.content)
    report.step("poll vor dem Claim → 202 pending")

    # 3. claim by a user
    await claim(started.code)
    report.step("Claim durch Nutzer")

    # 4. poll until the secret arrives (exactly once)
    deadline = time.monotonic() + poll_timeout
    claimed: PairingClaimed | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        r = await client.get(f"{API}/pairing/poll", params={"poll_token": started.poll_token})
        if r.status_code == 202:
            continue
        check(r.status_code == 200, f"poll: {r.status_code} {r.text}")
        check("mqtt" not in r.json(), "M1 pairing answer must not contain mqtt")
        claimed = PairingClaimed.model_validate_json(r.content)
        break
    check(claimed is not None, "pairing not claimed in time")
    assert claimed is not None
    report.tenant_id = claimed.tenant_id
    report.step("poll → Secret erhalten (ohne mqtt)")
    await asyncio.sleep(poll_interval)
    r = await client.get(f"{API}/pairing/poll", params={"poll_token": started.poll_token})
    check(r.status_code == 410, f"second poll must be 410, got {r.status_code}")
    check(
        ErrorResponse.model_validate_json(r.content).error.code == ErrorCode.PAIRING_CONSUMED,
        "code",
    )
    report.step("zweiter poll → 410 pairing_consumed")

    # 5. token
    token_req = DeviceTokenRequest(device_id=report.device_id, device_secret=claimed.device_secret)
    r = await client.post(f"{API}/device/token", json=token_req.model_dump(mode="json"))
    check(r.status_code == 200, f"device/token: {r.status_code} {r.text}")
    token = DeviceTokenResponse.model_validate_json(r.content)
    auth = {"Authorization": f"Bearer {token.access_token}"}
    report.step(f"device/token → JWT, {token.expires_in}s")

    # 6. reported (HTTP fallback, SPEC v0.3)
    reported = ReportedMessage.model_validate(
        {
            "id": ulid(),
            "ts": dt.datetime.now(dt.UTC),
            "data": {
                "agent_version": "0.0.0-fake",
                "hw_model": "fake-device",
                "applied_config_rev": 0,
                "applied_device_rev": 0,
                "storage": {"free_mb": 1024},
                "time_trusted": True,
                "playback": {"status": "stopped", "volume": 35},
            },
        }
    )
    r = await client.post(
        f"{API}/device/reported", json=reported.model_dump(mode="json"), headers=auth
    )
    check(r.status_code == 204, f"device/reported: {r.status_code} {r.text}")
    report.step("device/reported → 204")

    # 7. state (M1: always full)
    r = await client.get(
        f"{API}/device/state", params={"config_rev": 0, "device_rev": 0}, headers=auth
    )
    check(r.status_code == 200, f"device/state: {r.status_code} {r.text}")
    state = StateResponse.model_validate_json(r.content)
    check(state.full, "M1 state must be a full snapshot")
    report.config_rev = state.config_rev
    items = state.upserts.content_item
    up = state.upserts
    report.step(
        f"device/state → full, config_rev {state.config_rev}, device_rev {state.device_rev}, "
        f"{len(up.token)} Figuren, {len(up.content)} Inhalte, {len(items)} Titel"
    )

    # 8. assets: download, verify SHA-256 and size; Range and ETag on the first
    for item in items:
        if item.asset_sha256 in report.assets:
            continue
        r = await client.get(f"{API}/device/assets/{item.asset_sha256}", headers=auth)
        check(r.status_code == 200, f"asset {item.asset_sha256}: {r.status_code}")
        check(hashlib.sha256(r.content).hexdigest() == item.asset_sha256, "sha256 mismatch")
        check(len(r.content) == item.bytes, "size mismatch")
        check(r.headers.get("etag") == f'"{item.asset_sha256}"', "ETag must be the sha256")
        report.assets[item.asset_sha256] = len(r.content)
        if len(report.assets) == 1:
            report.asset_headers = {k.lower(): v for k, v in r.headers.items()}
            ranged = await client.get(
                f"{API}/device/assets/{item.asset_sha256}", headers=auth | {"Range": "bytes=0-99"}
            )
            check(ranged.status_code == 206, f"range request: {ranged.status_code}")
            check(ranged.content == r.content[:100], "range content mismatch")
            cached = await client.get(
                f"{API}/device/assets/{item.asset_sha256}",
                headers=auth | {"If-None-Match": f'"{item.asset_sha256}"'},
            )
            check(cached.status_code == 304, f"If-None-Match: {cached.status_code}")
    if expect_assets is not None:
        check(
            len(report.assets) == expect_assets,
            f"expected {expect_assets} assets, got {len(report.assets)}",
        )
    report.step(f"{len(report.assets)} Assets geladen, SHA-256 geprüft (Range 206, ETag 304)")

    # 9. event, then the same event again → duplicate
    event = TokenUnknownEvent.model_validate(
        {
            "id": ulid(),
            "ts": dt.datetime.now(dt.UTC),
            "boot_id": uuid.uuid4(),
            "mono_ms": 12345,
            "data": {"uid": "04FA6E00C0FFEE"},
        }
    )
    batch = EventBatchRequest.model_validate({"events": [event.model_dump(mode="json")]})
    for expected in ("accepted", "duplicate"):
        r = await client.post(
            f"{API}/device/events", json=batch.model_dump(mode="json"), headers=auth
        )
        check(r.status_code == 200, f"device/events: {r.status_code} {r.text}")
        result = EventBatchResponse.model_validate_json(r.content).results[0]
        check(result.status == expected, f"event status {result.status}, expected {expected}")
    report.step("device/events → accepted, erneut → duplicate")

    # 10. unpair; afterwards neither JWT nor secret work
    r = await client.post(f"{API}/device/unpair", headers=auth)
    check(r.status_code == 204, f"device/unpair: {r.status_code} {r.text}")
    r = await client.get(f"{API}/device/state", headers=auth)
    check(r.status_code == 401, f"state after unpair must be 401, got {r.status_code}")
    r = await client.post(f"{API}/device/token", json=token_req.model_dump(mode="json"))
    check(r.status_code == 401, f"token after unpair must be 401, got {r.status_code}")
    ErrorResponse.model_validate_json(r.content)
    report.step("device/unpair → 204, danach JWT und Secret ungültig (401)")
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--device-id", type=uuid.UUID)
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--expect-assets", type=int)
    p.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (test certificates)"
    )
    p.add_argument(
        "--auto-claim", action="store_true", help="claim via the user API instead of waiting"
    )
    p.add_argument("--email")
    p.add_argument("--tenant", help="tenant id for --auto-claim")
    p.add_argument("--name", default="Fake-Box")
    args = p.parse_args(argv)

    verify = not args.insecure

    async def go() -> Report:
        claim: ClaimFn = prompt_claim
        if args.auto_claim:
            password = os.environ.get("BOX_PASSWORD")
            if not (args.email and args.tenant and password):
                p.error("--auto-claim needs --email, --tenant and $BOX_PASSWORD")
            claim = await user_claim(
                args.base_url,
                email=args.email,
                password=password,
                tenant_id=args.tenant,
                name=args.name,
                verify=verify,
            )
        async with httpx.AsyncClient(base_url=args.base_url, verify=verify, timeout=60) as client:
            return await run(
                client,
                claim=claim,
                device_id=args.device_id,
                poll_interval=args.poll_interval,
                expect_assets=args.expect_assets,
            )

    print(f"Fake-Box gegen {args.base_url}")
    try:
        asyncio.run(go())
    except FlowError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 1
    print("Ablauf vollständig.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
