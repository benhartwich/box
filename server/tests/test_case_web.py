"""Box gestalten: the public configurator page, preview mesh and print files (docs/gehaeuse.md)."""

from __future__ import annotations

import io
import re
import zipfile

import httpx
import pytest
from fastapi import FastAPI

from myboxi_case.config import CaseConfig
from myboxi_case.export import read_preview
from myboxi_server.auth import ratelimit
from myboxi_server.auth.ratelimit import Limit

from .helpers import login, make_tenant


async def test_page_is_public_and_keeps_the_choices(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten?form=bear&name=Mia&color_body=braun")
    assert r.status_code == 200
    assert "Box gestalten" in r.text
    assert 'value="Mia"' in r.text
    assert 'name="form" value="bear" checked' in r.text
    assert 'name="color_body" value="braun" checked' in r.text
    assert 'href="/gestalten/download.zip?form=bear&amp;name=Mia&amp;color_body=braun"' in r.text
    script = re.search(r'<script type="module" src="(/static/case\.js\?v=[0-9a-f]{10})">', r.text)
    assert script
    assert "default-src 'self'" in r.headers["content-security-policy"]
    # Without an order address the request form stays off.
    assert "/gestalten/anfrage" not in r.text
    assert 'href="/login"' in r.text  # public header


async def test_colours_follow_the_form_until_chosen(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten?form=unicorn")
    assert 'name="color_body" value="weiss" checked' in r.text
    assert 'name="color_accent" value="sonne" checked' in r.text
    assert CaseConfig(form="unicorn").color("accent") == "#F2C66D"
    assert CaseConfig(form="unicorn", color_accent="rot").color("accent") == "#D64541"


async def test_page_for_signed_in_people(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    await login(client, t.owner_email)
    r = await client.get("/gestalten")
    assert r.status_code == 200
    assert "Abmelden" in r.text


async def test_page_explains_bad_choices(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten?name=" + "x" * 20)
    assert r.status_code == 422
    assert "höchstens 14 Zeichen" in r.text
    r = await client.get("/gestalten?form=castle")
    assert r.status_code == 422
    assert "Diese Auswahl gibt es nicht" in r.text
    r = await client.get("/gestalten?form=cube&power=powerbank")
    assert r.status_code == 200
    assert "Powerbank passt nur ins Radio" in r.text
    assert 'aria-disabled="true"' in r.text


async def test_preview_mesh_with_etag(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten/vorschau?form=cube&name=Jonas")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-encoding"] == "gzip"
    digest = CaseConfig(form="cube", name="Jonas").digest()
    assert r.headers["etag"] == f'"{digest}"'
    info, meshes = read_preview(r.content)  # httpx has decoded the gzip body
    assert info["size"] == [110.0, 110.0, 100.0]
    assert len(meshes) > 5
    again = await client.get(
        "/gestalten/vorschau?form=cube&name=Jonas", headers={"If-None-Match": f'"{digest}"'}
    )
    assert again.status_code == 304


async def test_preview_refuses_bad_input(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten/vorschau?form=cube&power=powerbank")
    assert r.status_code == 422
    assert "Powerbank" in r.text
    r = await client.get("/gestalten/vorschau?speaker=33")
    assert r.status_code == 422


async def test_download_zip(client: httpx.AsyncClient) -> None:
    r = await client.get("/gestalten/download.zip?form=bear&name=Mäxi")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="myboxi-bear-maexi.zip"'
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "myboxi-bear-maexi.3mf" in names
    readme = zipfile.ZipFile(io.BytesIO(r.content)).read("LIESMICH.txt").decode()
    assert "/gestalten?form=bear&name=M%C3%A4xi" in readme


async def test_builds_are_rate_limited_but_cache_hits_are_free(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "CASE_BUILD_PER_IP", (Limit(2, 3600),))
    assert (await client.get("/gestalten/vorschau?name=A")).status_code == 200
    assert (await client.get("/gestalten/vorschau?name=B")).status_code == 200
    blocked = await client.get("/gestalten/vorschau?name=C")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert (await client.get("/gestalten/vorschau?name=A")).status_code == 200  # cached


async def test_static_files_are_versioned(client: httpx.AsyncClient) -> None:
    """A stale cached stylesheet once made the preview canvas grow without end."""
    page = (await client.get("/gestalten")).text
    css = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{10})"', page)
    assert css
    r = await client.get(css.group(1))
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    plain = await client.get("/static/app.css")
    assert plain.headers["cache-control"] == "no-cache"
    vendor = await client.get("/static/vendor/three-0.186.1/OrbitControls.js")
    assert "immutable" in vendor.headers["cache-control"]


async def test_public_header_links_to_the_configurator(client: httpx.AsyncClient) -> None:
    r = await client.get("/login")
    assert 'href="/gestalten"' in r.text
