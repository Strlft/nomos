"""Smoke test for the redesigned GET /advisor portal page.

Asserts:
  * The page is served (200) with text/html.
  * The body wires the shared design system (nomos.css + nomos-ui.js) and
    the five redesigned views (Dashboard / Contracts / Audit / Notices /
    Library) consolidated from the legacy nine-view sidebar.
  * The body has shed the legacy AI-slop palette: no purple/blue/old-warm
    gradient colors, no glassmorphism backdrop filters, no Google-Fonts
    Inter / JetBrains imports.
  * The legacy nine-view sidebar has been collapsed: "Pending Actions",
    "Due Diligence", "Monitoring §3/§4/§5", "Netting Opinions" and the
    sidebar "+ New Contract" entry are gone.
  * The body wires the new v2 oracle endpoints used by the topbar chip,
    the Dashboard market data panel, and the Dashboard triggers panel.
  * The body still wires the legacy /api/contracts and /api/audit
    endpoint families — contract operations and audit trail continue to
    use those URLs.
  * The sign-out behavior is preserved: it clears nomos_role from
    localStorage and redirects to '/'.
  * The demo-mode toggle is preserved: the page references the
    /api/demo-mode endpoint and exposes a toggleDemoMode handler.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from backend.api import app
    return TestClient(app)


def test_advisor_portal_returns_200_html() -> None:
    r = _client().get("/advisor")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"unexpected content-type: {ct!r}"


def test_advisor_portal_wires_design_system_and_five_views() -> None:
    body = _client().get("/advisor").text
    for needle in (
        "Nomos",
        "Advisor",
        "Dashboard",
        "Contracts",
        "Audit",
        "Notices",
        "Library",
        "nomos.css",
        "nomos-ui.js",
    ):
        assert needle in body, f"expected page to contain {needle!r}"


def test_advisor_portal_exposes_oracle_as_sidebar_entry() -> None:
    body = _client().get("/advisor").text
    assert 'data-view="oracle"' in body, (
        "advisor portal must expose Oracle as an internal sidebar entry"
    )
    assert "Oracle" in body, "advisor portal must surface the Oracle label"


def test_advisor_portal_has_shed_legacy_palette_and_fonts() -> None:
    body = _client().get("/advisor").text
    forbidden = (
        "linear-gradient",
        "backdrop-filter",
        "#a78bfa",
        "#60a5fa",
        "#c4956a",
        "Inter:wght",
        "JetBrains+Mono",
    )
    for token in forbidden:
        assert token not in body, (
            f"advisor portal must not reference legacy token {token!r}"
        )


def test_advisor_portal_has_collapsed_legacy_sidebar() -> None:
    body = _client().get("/advisor").text
    forbidden = (
        "Pending Actions",
        "Due Diligence",
        "+ New Contract",
        "Monitoring §3/§4/§5",
        "Netting Opinions",
    )
    for token in forbidden:
        assert token not in body, (
            f"advisor portal must not surface legacy sidebar entry {token!r}"
        )


def test_advisor_portal_wires_v2_oracle_endpoints() -> None:
    body = _client().get("/advisor").text
    for needle in (
        "/api/v2/oracle/health",
        "/api/v2/oracle/attestations/latest",
        "/api/v2/oracle/triggers",
        "/api/v2/oracle/chain/verify",
    ):
        assert needle in body, (
            f"advisor portal must reference new v2 endpoint {needle!r}"
        )


def test_advisor_portal_still_wires_legacy_contract_and_audit_endpoints() -> None:
    body = _client().get("/advisor").text
    assert "/api/contracts" in body, (
        "advisor portal must still call the legacy /api/contracts endpoints"
    )
    assert "/api/audit" in body, (
        "advisor portal must still call the legacy /api/audit endpoint"
    )


def test_advisor_portal_preserves_sign_out_behavior() -> None:
    body = _client().get("/advisor").text
    assert "localStorage.removeItem('nomos_role')" in body, (
        "sign-out must clear nomos_role from localStorage"
    )
    assert "window.location.href = '/'" in body or "window.location.href='/'" in body, (
        "sign-out must redirect to '/'"
    )


def test_advisor_portal_preserves_demo_mode_toggle() -> None:
    body = _client().get("/advisor").text
    assert "/api/demo-mode" in body, (
        "advisor portal must call the /api/demo-mode endpoint"
    )
    assert "toggleDemoMode" in body, (
        "advisor portal must expose a toggleDemoMode handler"
    )
