"""Smoke test for the redesigned GET /client portal page.

Asserts:
  * The page is served (200) with text/html.
  * The body wires the shared design system (nomos.css + nomos-ui.js) and
    the four redesigned views (Today / Contracts / Documents / Profile).
  * The body has shed the legacy AI-slop palette: no purple/blue/old-warm
    gradient colors, no glassmorphism, no Google-Fonts Inter / JetBrains.
  * The body wires the new v2 oracle endpoints used by the Today view and
    by the contract detail oracle reference panel.
  * The body still wires the legacy /api/contracts endpoint family —
    contract detail, signing, due diligence, comments, and PDFs continue
    to use those URLs.
  * The sign-out behavior is preserved: it clears nomos_role from
    localStorage and redirects to '/'.
  * The legacy 7-view sidebar has been collapsed: "My Swaps" and "Alerts"
    no longer appear as labels on the page.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from backend.api import app
    return TestClient(app)


def test_client_portal_returns_200_html() -> None:
    r = _client().get("/client")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"unexpected content-type: {ct!r}"


def test_client_portal_wires_design_system_and_four_views() -> None:
    body = _client().get("/client").text
    for needle in (
        "Nomos",
        "Today",
        "Contracts",
        "Documents",
        "Profile",
        "nomos.css",
        "nomos-ui.js",
    ):
        assert needle in body, f"expected page to contain {needle!r}"


def test_client_portal_exposes_oracle_as_sidebar_entry() -> None:
    body = _client().get("/client").text
    assert 'data-view="oracle"' in body, (
        "client portal must expose Oracle as an internal sidebar entry"
    )
    assert "Oracle" in body, "client portal must surface the Oracle label"


def test_client_portal_has_shed_legacy_palette_and_fonts() -> None:
    body = _client().get("/client").text
    forbidden = (
        "linear-gradient",
        "backdrop-filter",
        "#a78bfa",
        "#60a5fa",
        "#c4956a",
        "My Swaps",
        "Alerts",
        "Inter:wght",
        "JetBrains+Mono",
    )
    for token in forbidden:
        assert token not in body, (
            f"client portal must not reference legacy token {token!r}"
        )


def test_client_portal_wires_v2_oracle_endpoints() -> None:
    body = _client().get("/client").text
    for needle in (
        "/api/v2/oracle/health",
        "/api/v2/oracle/attestations/latest",
        "/api/v2/oracle/triggers",
        "/api/v2/oracle/chain/verify",
    ):
        assert needle in body, (
            f"client portal must reference new v2 endpoint {needle!r}"
        )


def test_client_portal_still_wires_legacy_contract_endpoints() -> None:
    body = _client().get("/client").text
    assert "/api/contracts" in body, (
        "client portal must still call the legacy /api/contracts endpoints"
    )


def test_client_portal_preserves_sign_out_behavior() -> None:
    body = _client().get("/client").text
    assert "localStorage.removeItem('nomos_role')" in body, (
        "sign-out must clear nomos_role from localStorage"
    )
    # The redirect target is '/' (the login picker).
    assert "window.location.href = '/'" in body or "window.location.href='/'" in body, (
        "sign-out must redirect to '/'"
    )
