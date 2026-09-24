"""Acceptance: device API responses are declared and validated with box_protocol models."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from box_server.api.device.router import router as device_router


def test_device_routes_use_protocol_models(app: FastAPI) -> None:
    del app
    checked = 0
    for ctx in iter_route_contexts(device_router.routes):
        model = ctx.response_model
        if model is None:
            # 204 responses and the asset download carry no JSON body.
            assert ctx.status_code == 204 or "{sha256}" in (ctx.path or ""), ctx.path
            continue
        assert model.__module__.startswith("box_protocol."), (ctx.path, model)
        checked += 1
    assert checked >= 5
