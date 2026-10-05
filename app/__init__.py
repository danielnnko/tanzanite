"""
app/__init__.py
───────────────
Flask application factory.
"""

import logging
import os
import secrets
from pathlib import Path

from flask import Flask
from flask_login import LoginManager

from tansat import db as tansat_db
from tansat import auth as tansat_auth
from tansat import acquisition as acq_svc


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # ── Secret key ──────────────────────────────────────────────
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        # Only for development — warn loudly
        logging.warning(
            "SECRET_KEY not set in environment. "
            "Generating a random key — sessions will be invalidated on restart."
        )
        secret_key = secrets.token_hex(32)
    app.secret_key = secret_key

    # ── Config overrides ─────────────────────────────────────────
    # DATA_DIR must be ABSOLUTE: Flask's send_file() resolves relative
    # paths against the app package folder (app/data/…) while everything
    # else (route exists() checks, the acquisition service) uses the
    # process CWD (project root). That mismatch made every log/image
    # download fail with a FileNotFoundError → HTTP 500 → the browser
    # reported "site was not available" instead of downloading.
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config["DATA_DIR"] = str(data_dir)
    app.config["DATABASE_URL"] = os.environ.get(
        "DATABASE_URL",
        "postgresql://localhost/tansat"
    )
    if config:
        app.config.update(config)

    # ── Database ─────────────────────────────────────────────────
    tansat_db.init_db(app.config["DATABASE_URL"])

    # Bootstrap admin account (once) ─────────────────────────────
    _bootstrap_admin(app)

    # ── Acquisition service ───────────────────────────────────────
    svc = acq_svc.init_service(data_dir=app.config["DATA_DIR"])
    acq_svc._resolve_control_lock()

    # Sync GS config from DB into service
    settings = tansat_db.get_all_settings()
    try:
        svc.update_gs_config(
            gs_lat=float(settings.get("gs_lat",  -6.813737)),
            gs_lon=float(settings.get("gs_lon",  39.280427)),
            gs_alt=float(settings.get("gs_alt",  30.0)),
            alt_offset=float(settings.get("altitude_offset", 0.0)),
        )
    except (ValueError, TypeError) as e:
        logging.warning("Failed to sync GS config from DB: %s", e)

    # Sync attitude filter tuning from DB into service
    try:
        svc.update_attitude_config(
            alpha=float(settings.get("attitude_alpha", 0.98)),
            gyro_scale=float(settings.get("gyro_scale", 1.0)),
        )
    except (ValueError, TypeError) as e:
        logging.warning("Failed to sync attitude tuning from DB: %s", e)

    # ── Orbital tracking engine ────────────────────────────────────
    # Started lazily via svc.tracking_engine() on first use; here we start
    # it eagerly so the 1 Hz tracking SSE stream begins with the app.
    try:
        svc.tracking_engine()
    except Exception as e:
        logging.warning("Tracking engine failed to start: %s", e)

    # ── Flask-Login ───────────────────────────────────────────────
    tansat_auth.login_manager.login_view = "auth.login"
    tansat_auth.login_manager.login_message_category = "warning"
    tansat_auth.login_manager.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────
    from app.routes.auth  import auth_bp
    from app.routes.pages import pages_bp
    from app.routes.api   import api_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp,   url_prefix="/api")
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # ── Global forced-password-change gate ─────────────────────────
    # Applies to EVERY authenticated route (not just admin-only ones).
    # Allow-list: the change-password page itself, logout, and static
    # assets — everything else redirects here until the password is changed.
    from flask import request, redirect, url_for
    from flask_login import current_user

    _ALLOWED_ENDPOINTS = {"auth.change_password", "auth.logout", "static"}

    @app.before_request
    def _force_password_change():
        if not current_user.is_authenticated:
            return None
        if request.endpoint in _ALLOWED_ENDPOINTS:
            return None
        if getattr(current_user, "must_change_password", False):
            return redirect(url_for("auth.change_password"))
        return None

    # ── Global template context (station name/location for top bar) ─
    @app.context_processor
    def _inject_station_info():
        s = tansat_db.get_all_settings()
        return {
            "station_name": s.get("station_name", "Ground Station"),
            "station_location": s.get("station_location", "Dar es Salaam, Tanzania"),
            "gs_lat_g": s.get("gs_lat", -6.813737),
            "gs_lon_g": s.get("gs_lon", 39.280427),
        }

    return app


def _bootstrap_admin(app: Flask) -> None:
    """Create the initial admin account if no users exist. Print temp password to console."""
    try:
        users = tansat_db.list_users()
        if users:
            return
        from tansat.auth import hash_password, generate_temp_password
        temp_pw = generate_temp_password(14)
        tansat_db.create_user(
            username="admin",
            email=None,
            password_hash=hash_password(temp_pw),
            role="admin",
            must_change_password=True,
        )
        tansat_db.audit(
            actor_username="SYSTEM",
            action="bootstrap_admin_created",
            detail="Initial admin account created on first run",
        )
        print("\n" + "═" * 60)
        print("  TANZANITESAT — First-run bootstrap")
        print("  Username : admin")
        print(f"  Password : {temp_pw}")
        print("  ⚠  Change this password immediately after first login!")
        print("═" * 60 + "\n")
    except Exception as e:
        logging.error("Bootstrap admin failed: %s", e)
