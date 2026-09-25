"""Acceptance: tools/fake-device runs the complete device flow against a real uvicorn."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
from fastapi import FastAPI

from .helpers import PASSWORD, make_tenant, seed_library

FAKE_DEVICE = Path(__file__).resolve().parents[2] / "tools" / "fake-device" / "fake_device.py"


def _load_fake_device() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_device", FAKE_DEVICE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_device"] = module
    spec.loader.exec_module(module)
    return module


async def test_fake_device_complete_flow(live_server: str, app: FastAPI) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id, n_items=3)
    fake = _load_fake_device()
    claim = await fake.user_claim(
        live_server, email=t.owner_email, password=PASSWORD, tenant_id=str(t.tenant_id), name="E2E"
    )
    async with httpx.AsyncClient(base_url=live_server, timeout=30) as client:
        report = await fake.run(client, claim=claim, poll_interval=2.1, expect_assets=3)
    assert report.tenant_id == t.tenant_id
    assert set(report.assets) == set(lib.shas)
    assert len(report.steps) == 11
