"""Smoke test for the new GET /oracle page route.

Asserts:
  * The page is served (200) with text/html.
  * The body wires up the shared design system and the v2 API.
  * The body does NOT reference any legacy oracle_v3 path or the static
    fallback constant — confirming isolation from the legacy oracle.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from backend.api import app
    return TestClient(app)


def test_oracle_page_returns_200_html() -> None:
    r = _client().get("/oracle")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"unexpected content-type: {ct!r}"


def test_oracle_page_wires_design_system_and_v2_api() -> None:
    body = _client().get("/oracle").text
    for needle in ("Nomos", "Oracle", "nomos.css", "nomos-ui.js", "/api/v2/oracle/"):
        assert needle in body, f"expected page to contain {needle!r}"


def test_oracle_page_does_not_leak_legacy_oracle() -> None:
    body = _client().get("/oracle").text
    forbidden = (
        "oracle_v3",
        "_STATIC_FALLBACK",
        "/api/oracle/latest",
        "/api/oracle/rates",
        "/api/oracle/events",
        "/api/oracle/regulatory",
    )
    for token in forbidden:
        assert token not in body, f"page must not reference legacy {token!r}"
