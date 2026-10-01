"""
app/routes/pages.py
────────────────────
All HTML page routes (server-rendered shells that fill live via SSE).
"""

from flask import Blueprint, redirect, render_template, url_for
from flask_login import login_required

from tansat import db as tansat_db
from tansat import acquisition as acq_svc
from tansat.auth import require_role

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/favicon.ico")
def favicon_ico():
    """Legacy favicon path: browsers still request /favicon.ico when no
    explicit <link rel="icon"> matches — redirect to the SVG icon."""
    return redirect(url_for("static", filename="favicon.svg"), code=302)


def _svc():
    return acq_svc.get_service()


# ── Dashboard ──────────────────────────────────────────────────────
@pages_bp.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html",
                           title="Dashboard",
                           status=_svc().get_status())


# ── Full Telemetry ─────────────────────────────────────────────────
@pages_bp.route("/telemetry")
@login_required
def telemetry():
    return render_template("telemetry.html",
                           title="Full Telemetry",
                           status=_svc().get_status())


# ── Analytics Logs ─────────────────────────────────────────────────
@pages_bp.route("/logs")
@login_required
def logs():
    return render_template("logs.html", title="Analytics Logs")


# ── GIS Map View ───────────────────────────────────────────────────
@pages_bp.route("/map")
@login_required
def map_view():
    settings = tansat_db.get_all_settings()
    return render_template("map.html",
                           title="GIS Map View",
                           gs_lat=settings.get("gs_lat", -6.813737),
                           gs_lon=settings.get("gs_lon", 39.280427))


# ── Satellite Tracking ─────────────────────────────────────────────
@pages_bp.route("/tracking")
@login_required
def tracking():
    return render_template("tracking.html",
                           title="Satellite Tracking",
                           status=_svc().get_status())


# ── 3D Attitude View ───────────────────────────────────────────────
@pages_bp.route("/attitude")
@login_required
def attitude():
    return render_template("attitude.html", title="3D Attitude View")


# ── Earth Orbit View (§8) ──────────────────────────────────────────
@pages_bp.route("/earth-orbit")
@login_required
def earth_orbit():
    settings = tansat_db.get_all_settings()
    return render_template("earth_orbit.html",
                           title="Earth Orbit View",
                           gs_lat=settings.get("gs_lat", -6.813737),
                           gs_lon=settings.get("gs_lon", 39.280427))


# ── Payload Feed ───────────────────────────────────────────────────
@pages_bp.route("/payload")
@login_required
def payload():
    return render_template("payload.html", title="Payload Feed")
