"""Acceptance: tools/fake-device runs the complete device flow against a real uvicorn."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from box_server.app import create_app
from box_server.settings import Settings

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def live_server(settings: Settings, engine: AsyncEngine) -> AsyncIterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


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
