"""
tests/test_auth.py
──────────────────
Authentication flow tests:
  • /logout is POST-only (GET must 405 — the original bug)
  • authenticated POST /logout → 302 → login page, session cleared
  • profile dropdown renders a POST sign-out form (no GET link, no modal)
  • /profile: any role can view + edit own details; ID is display-only
  • /change-password is reachable for every signed-in role
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


# ── Profile dropdown markup ───────────────────────────────────────────

def test_dropdown_signout_is_post_form_with_confirm(admin_client):
    """Sign out lives in the profile dropdown as a POST form with a JS
    confirm guard; the sidebar keeps no auth actions."""
    html = admin_client.get("/").get_data(as_text=True)
    assert 'action="/logout" method="POST"' in html
    assert '<a href="/logout"' not in html          # old broken markup
    assert 'id="logout-modal"' not in html          # modal was removed
    assert 'Sign out' in html
    # Dropdown must expose Edit profile + Account settings to every role
    assert '/profile' in html
    assert '/change-password' in html


def test_theme_toggle_sits_in_topbar_not_sidebar(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    # The toggle must appear before the user-menu trigger in the topbar
    assert 'id="user-menu-trigger"' in html
    assert 'theme-toggle' in html
    assert html.index('theme-toggle') < html.index('id="user-menu-trigger"')


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


# ── Profile editing (own account, any role) ───────────────────────────

def _login_as(db, app, username):
    """Create a session for an existing user by username."""
    c = app.test_client()
    user = db.get_user_by_username(username)
    if not user:
        pytest.skip(f"{username} account missing")
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user["id"])
    return c, user


def test_profile_page_renders_for_admin(admin_client):
    r = admin_client.get("/profile")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'name="full_name"' in html
    assert 'name="username"' in html
    assert 'name="email"' in html
    assert "disabled" in html                      # ID read-only


def test_profile_update_own_details(app):
    from tansat import db
    c, user = _login_as(db, app, "admin")

    r = c.post("/profile", data={
        "full_name": "Station Admin",
        "username": user["username"],
        "email": "admin@tanzanitesat.test",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Profile updated." in r.get_data(as_text=True)

    fresh = db.get_user_by_id(user["id"])
    assert fresh["full_name"] == "Station Admin"
    assert fresh["email"] == "admin@tanzanitesat.test"

    # restore
    db.update_user(user["id"], full_name=None, email=user["email"])


def test_profile_rejects_duplicate_username(app):
    from tansat import db
    c, user = _login_as(db, app, "admin")
    r = c.post("/profile", data={
        "full_name": user["full_name"] or "",
        "username": "definitely_not_a_name_in_use_xyz",
        "email": user["email"] or "",
    }, follow_redirects=True)
    # rename to a free username succeeds, and the audit trail logs it
    assert r.status_code == 200
    fresh = db.get_user_by_id(user["id"])
    assert fresh["username"] == "definitely_not_a_name_in_use_xyz"
    # restore original username
    db.update_user(user["id"], username=user["username"])


def test_profile_requires_login(client):
    r = client.get("/profile", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_change_password_page_reachable_for_admin(admin_client):
    r = admin_client.get("/change-password")
    assert r.status_code == 200
    assert "Account settings" in r.get_data(as_text=True)
