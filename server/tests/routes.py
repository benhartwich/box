"""Route introspection for the isolation and role tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from box_server.domain.authz import Perm

PARAM_RE = re.compile(r"{(\w+)}")


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str
    perm: Perm | None

    @property
    def params(self) -> list[str]:
        return PARAM_RE.findall(self.path)

    def url(self, values: dict[str, str]) -> str:
        return PARAM_RE.sub(lambda m: values[m.group(1)], self.path)


def _perm(dependant: Dependant) -> Perm | None:
    for dep in dependant.dependencies:
        perm = getattr(dep.call, "required_perm", None)
        if isinstance(perm, Perm):
            return perm
        found = _perm(dep)
        if found is not None:
            return found
    return None


def routes(app: FastAPI) -> Iterator[RouteInfo]:
    """All API routes with their effective (prefixed) paths and dependencies."""
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        dependant: Dependant = ctx.dependant
        path = ctx.path
        assert path is not None
        for method in sorted(ctx.methods or ()):
            yield RouteInfo(method, path, _perm(dependant))


def tenant_routes(app: FastAPI) -> list[RouteInfo]:
    """Every route addressed by a tenant id (web UI and user API)."""
    return [r for r in routes(app) if "tid" in r.params]
