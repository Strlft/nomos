"""Smoke test for the GET / login page.

Asserts:
  * The page is served (200) with text/html.
  * The body wires up the shared design system (nomos.css + nomos-ui.js).
  * The body shows the two role labels (Client / Advisor).
  * The body has shed the legacy AI-slop palette: no purple/blue gradient
    colors, no glassmorphism (linear-gradient / backdrop-filter / #a78bfa /
    #60a5fa).
  * The role-redirect JavaScript is preserved bit-for-bit: it writes
    ``nomos_role`` to localStorage on click, navigates via
    ``window.location.href``, and reads ``nomos_role`` on page load to
    auto-redirect returning users.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from backend.api import app
    return TestClient(app)


def test_login_page_returns_200_html() -> None:
    r = _client().get("/")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"unexpected content-type: {ct!r}"


def test_login_page_wires_design_system_and_roles() -> None:
    body = _client().get("/").text
    for needle in ("Nomos", "nomos.css", "nomos-ui.js", "Client", "Advisor"):
        assert needle in body, f"expected page to contain {needle!r}"


def test_login_page_has_shed_legacy_palette() -> None:
    body = _client().get("/").text
    forbidden = (
        "linear-gradient",
        "backdrop-filter",
        "#a78bfa",
        "#60a5fa",
    )
    for token in forbidden:
        assert token not in body, (
            f"login page must not reference legacy AI-slop token {token!r}"
        )


def test_login_page_preserves_role_redirect_behavior() -> None:
    body = _client().get("/").text
    # On click: persist role + navigate.
    assert "localStorage.setItem('nomos_role'" in body, (
        "login page must persist the role to localStorage on click"
    )
    assert "window.location.href" in body, (
        "login page must navigate via window.location.href"
    )
    # On load: auto-redirect returning users.
    assert "localStorage.getItem('nomos_role')" in body, (
        "login page must read nomos_role from localStorage on load"
    )
    assert "/advisor" in body and "/client" in body, (
        "login page must redirect to /advisor and /client"
    )
