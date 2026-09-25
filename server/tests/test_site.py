"""The landing page (site/) loads nothing from third parties (privacy promise on the page)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2] / "site"
PAGES = sorted(SITE.glob("*.html"))


def test_pages_exist() -> None:
    assert {p.name for p in PAGES} >= {"index.html", "impressum.html", "datenschutz.html"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_third_party_resources(page: Path) -> None:
    html = page.read_text()
    assert "<script" not in html
    assert "style=" not in html  # the CSP allows stylesheets from the site only
    for tag in re.findall(r"<(?:link|img|source|iframe)\b[^>]*>", html):
        for url in re.findall(r'(?:href|src)="([^"]+)"', tag):
            assert url.startswith("/"), f"{page.name}: {tag}"
            if tag.startswith("<img"):
                assert (SITE / url.lstrip("/")).is_file(), url


def test_stylesheet_uses_local_fonts_only() -> None:
    css = (SITE / "style.css").read_text()
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert urls
    assert all(u.strip("\"'").startswith("fonts/") for u in urls)
    for font in ("fonts/figtree-latin-wght-normal.woff2", "fonts/fraunces-latin-wght-normal.woff2"):
        assert (SITE / font).is_file()
