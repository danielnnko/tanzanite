"""
tests/test_auth.py
──────────────────
Authentication flow tests:
  • /logout is POST-only (GET must 405 — the original bug)
  • authenticated POST /logout → 302 → login page, session cleared
  • sidebar renders a POST form (not a GET link) with confirm modal
  • template audit: NO href= link anywhere may target a POST-only route

Requires the real DATABASE_URL (PostgreSQL). Skips automatically when the
database is unreachable so `pytest` still passes on machines without a DB.
"""
import os
import re
import pytest

# Page routes rendered through base.html — audited for GET links to POST routes
PAGE_ROUTES = ["/", "/telemetry", "/logs", "/map",
               "/tracking", "/attitude", "/earth-orbit", "/payload"]


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            with open(env_path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        url = line.split("=", 1)[1].strip()
                        break
    return url


_url = _database_url()
if _url:
    # create_app() reads DATABASE_URL from the process env — make sure the
    # project .env value is honoured even when pytest is run bare.
    os.environ.setdefault("DATABASE_URL", _url)


@pytest.fixture(scope="module")
def app():
    try:
        from app import create_app
        application = create_app()
    except Exception as exc:            # DB down, missing driver, etc.
        pytest.skip(f"cannot create app: {exc}")
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app):
    """Client authenticated as the admin user via the Flask-Login session."""
    from tansat import db
    c = app.test_client()
    admin = db.get_user_by_username("admin")
    if not admin:
        pytest.skip("admin account missing")
    with c.session_transaction() as sess:
        sess["_user_id"] = str(admin["id"])
    return c


# ── Route behavior ────────────────────────────────────────────────────

def test_logout_is_post_only(client):
    """GET /logout must be 405 — this is the original bug regression guard."""
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 405


def test_anonymous_logout_redirects_to_login(client):
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_authenticated_logout_returns_login_page(admin_client):
    assert admin_client.get("/").status_code == 200

    r = admin_client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]

    page = admin_client.get(r.headers["Location"])
    assert page.status_code == 200
    assert "login" in page.get_data(as_text=True).lower()

    # Session must actually be cleared after logout
    with admin_client.session_transaction() as sess:
        assert "_user_id" not in sess


# ── Sidebar markup ────────────────────────────────────────────────────

def test_sidebar_logout_is_post_form_with_modal(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    assert 'action="/logout" method="POST"' in html
    assert '<a href="/logout"' not in html          # old broken markup
    assert 'id="logout-modal"' in html              # confirmation modal present


def test_logout_modal_confirm_bypasses_submit_handler(admin_client):
    """
    Regression: the modal's confirm button must NOT be a submit button tied to
    the intercepted form. The form's submit handler exists only to SHOW the
    modal (it calls preventDefault), so a second submission through that
    handler would cancel the confirmed logout and the modal would never close
    (the "clicking Log out does nothing" bug).
    """
    html = admin_client.get("/").get_data(as_text=True)
    # Confirm button is a plain button driven by JS, not a submit button
    assert re.search(r'<button[^>]*id="logout-confirm"[^>]*type="button"', html) or \
           re.search(r'<button[^>]*type="button"[^>]*id="logout-confirm"', html)
    assert 'type="submit" form="logout-form"' not in html   # the buggy pattern
    # Confirmed logout posts via form.submit(), which skips submit handlers
    assert "form.submit()" in html


# ── Automated template audit ──────────────────────────────────────────

def _post_only_endpoints(flask_app) -> set[str]:
    """Every URL rule that accepts POST and nothing else."""
    out = set()
    for rule in flask_app.url_map.iter_rules():
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if methods == {"POST"}:
            out.add(rule.rule)
    return out


def test_no_get_links_to_post_only_endpoints(app, admin_client):
    """
    A plain <a href="..."> performs a GET request. Linking to a POST-only
    route guarantees a 405 (this is exactly how the logout bug happened).
    Every rendered page must avoid href= links to POST-only endpoints.
    """
    post_only = _post_only_endpoints(app)
    assert "/logout" in post_only          # sanity: guard rule is active
    assert "/admin/accounts/create" in post_only

    violations = []
    for route in PAGE_ROUTES:
        resp = admin_client.get(route)
        if resp.status_code != 200:
            continue
        for href in re.findall(r'href="([^"]+)"', resp.get_data(as_text=True)):
            path = href.split("?")[0]
            if path in post_only:
                violations.append((route, href))

    assert not violations, \
        f"GET links to POST-only routes found: {violations}"
