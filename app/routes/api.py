"""
app/routes/api.py
─────────────────
All /api/* endpoints:
  GET  /api/stream          — Server-Sent Events
  GET  /api/status          — snapshot JSON
  GET  /api/history         — chart back-fill
  GET  /api/ports           — serial ports visible to server
  POST /api/control/take    — acquire control lock
  POST /api/control/release — release control lock
  POST /api/control/heartbeat — refresh TTL
  POST /api/acquisition/start — start serial reader
  POST /api/acquisition/stop  — stop serial reader
  POST /api/uplink           — send uplink command
  GET  /api/logs/<name>      — download CSV session log
  GET  /api/logs/<name>.preview — first 50 lines preview
  GET  /api/export/orb.xlsx  — orbit data Excel export
"""

import io
import json
import logging
import os
import time
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, g,
                   jsonify, request, send_file, stream_with_context)
from flask_login import current_user, login_required

from tansat import acquisition as acq_svc
from tansat import db as tansat_db
from tansat.auth import require_role

api_bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────
def ok(**kwargs):
    return jsonify({"ok": True, **kwargs})


def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _service():
    return acq_svc.get_service()


def _data_dir() -> Path:
    return Path(current_app.config.get("DATA_DIR", "data"))


# ── SSE Stream ─────────────────────────────────────────────────────
@api_bp.route("/stream")
@login_required
def stream():
    """
    Server-Sent Events endpoint. One long-lived connection per browser tab.
    Each client gets its own bounded queue; slow clients drop oldest messages.
    """
    svc = _service()
    cid, q = svc.clients.register()

    # Send an initial status event immediately so the client knows state
    initial = json.dumps(svc.get_status())

    def generate():
        try:
            # Initial handshake
            yield f"event: status\ndata: {initial}\n\n"
            while True:
                try:
                    payload = q.get(timeout=20.0)
                    yield payload
                except Exception:
                    # Timeout → send keep-alive comment
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        finally:
            svc.clients.unregister(cid)
            logger.debug("SSE client %d disconnected", cid)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # Disable nginx buffering
        },
    )


# ── Status snapshot ─────────────────────────────────────────────────
@api_bp.route("/status")
@login_required
def status():
    return ok(**_service().get_status())


# ── History back-fill ───────────────────────────────────────────────
@api_bp.route("/history")
@login_required
def history():
    limit = request.args.get("limit", 300, type=int)
    limit = min(limit, 500)
    packets = _service().get_history(limit=limit)
    return ok(packets=packets, count=len(packets))


# ── Serial ports ────────────────────────────────────────────────────
@api_bp.route("/ports")
@login_required
def ports():
    """Lists serial ports visible to the **server** process, not the operator's laptop."""
    return ok(ports=acq_svc.list_serial_ports())


# ── Control lock ────────────────────────────────────────────────────
@api_bp.route("/control/take", methods=["POST"])
@login_required
def control_take():
    if current_user.role not in ("admin", "operator"):
        return err("Insufficient role", 403)
    svc = _service()
    force = request.json.get("force", False) if request.is_json else False
    if force and current_user.role != "admin":
        return err("Only admins may force-seize the control lock", 403)
    success, msg = svc.control_lock.take(current_user.username, force=force)
    if success:
        tansat_db.audit(
            actor_username=current_user.username,
            action="control_take" + ("_force" if force else ""),
            source_ip=request.remote_addr,
        )
        svc.clients.broadcast("control", svc.control_lock.status())
    return (ok(message=msg) if success else err(msg, 409))


@api_bp.route("/control/release", methods=["POST"])
@login_required
def control_release():
    svc = _service()
    force = request.json.get("force", False) if request.is_json else False
    if force and current_user.role != "admin":
        return err("Only admins may force-release the control lock", 403)
    success, msg = svc.control_lock.release(current_user.username, force=force)
    if success:
        tansat_db.audit(
            actor_username=current_user.username,
            action="control_release" + ("_force" if force else ""),
            source_ip=request.remote_addr,
        )
        svc.clients.broadcast("control", svc.control_lock.status())
    return (ok(message=msg) if success else err(msg, 409))


@api_bp.route("/control/heartbeat", methods=["POST"])
@login_required
def control_heartbeat():
    success, msg = _service().control_lock.heartbeat(current_user.username)
    return (ok(message=msg) if success else err(msg, 409))


# ── Acquisition start / stop ────────────────────────────────────────
@api_bp.route("/acquisition/start", methods=["POST"])
@login_required
def acquisition_start():
    if current_user.role not in ("admin", "operator"):
        return err("Insufficient role", 403)
    svc = _service()
    if not svc.control_lock.is_held_by(current_user.username):
        return err("You must hold the control lock to start acquisition", 403)

    data = request.json or {}
    port = data.get("port", "")
    baud = int(data.get("baud", tansat_db.get_setting("baud_rate", 9600)))
    if not port:
        return err("port is required")

    success, msg = svc.start(port=port, baud=baud)
    if success:
        tansat_db.audit(
            actor_username=current_user.username,
            action="acquisition_start",
            target=port,
            source_ip=request.remote_addr,
            detail=f"baud={baud}",
        )
    return (ok(message=msg) if success else err(msg))


@api_bp.route("/acquisition/stop", methods=["POST"])
@login_required
def acquisition_stop():
    if current_user.role not in ("admin", "operator"):
        return err("Insufficient role", 403)
    svc = _service()
    if not svc.control_lock.is_held_by(current_user.username):
        return err("You must hold the control lock to stop acquisition", 403)

    success, msg = svc.stop()
    if success:
        tansat_db.audit(
            actor_username=current_user.username,
            action="acquisition_stop",
            source_ip=request.remote_addr,
        )
    return (ok(message=msg) if success else err(msg))


# ── Uplink command ──────────────────────────────────────────────────
@api_bp.route("/uplink", methods=["POST"])
@login_required
def uplink():
    if current_user.role not in ("admin", "operator"):
        return err("Insufficient role", 403)
    svc = _service()
    if not svc.control_lock.is_held_by(current_user.username):
        return err("You must hold the control lock to transmit uplink", 403)

    data = request.json or {}
    command = (data.get("command") or "").strip()
    if not command:
        return err("command is required")
    if len(command) > 200:
        return err("command too long (max 200 chars)")

    success, msg = svc.send_uplink(command)
    if success:
        tansat_db.audit(
            actor_username=current_user.username,
            action="uplink_command",
            source_ip=request.remote_addr,
            detail=f"CMD: {command}",
        )
    return (ok(message=msg) if success else err(msg))


# ── Session logs ─────────────────────────────────────────────────────
@api_bp.route("/logs")
@login_required
def logs_list():
    data_dir = _data_dir()
    files = []
    for f in sorted(data_dir.glob("session_*.csv"), reverse=True):
        files.append({
            "name":  f.name,
            "size":  f.stat().st_size,
            "mtime": f.stat().st_mtime,
        })
    return ok(logs=files)


@api_bp.route("/logs/<name>")
@login_required
def logs_download(name: str):
    # Sanitise: only allow session_*.csv filenames
    if not name.startswith("session_") or not name.endswith(".csv") or "/" in name:
        abort(400)
    path = _data_dir() / name
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=name,
                     mimetype="text/csv")


@api_bp.route("/logs/<name>.preview")
@login_required
def logs_preview(name: str):
    if not name.startswith("session_") or not name.endswith(".csv") or "/" in name:
        abort(400)
    path = _data_dir() / name
    if not path.exists():
        abort(404)
    lines = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= 51:  # header + 50 rows
                break
            lines.append(line.rstrip())
    return ok(lines=lines)


# ── Payload images ───────────────────────────────────────────────────
@api_bp.route("/images")
@login_required
def images_list():
    img_dir = _data_dir() / "images"
    files = []
    if img_dir.exists():
        for f in sorted(img_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True):
            files.append({
                "name":  f.name,
                "size":  f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
    return ok(images=files)


@api_bp.route("/images/<name>")
@login_required
def images_download(name: str):
    # Sanitise: only allow plain *.jpg filenames, no path traversal
    if not name.endswith(".jpg") or "/" in name or "\\" in name or ".." in name:
        abort(400)
    path = _data_dir() / "images" / name
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=False, download_name=name)


# ── Orbit Excel export ───────────────────────────────────────────────
@api_bp.route("/export/orb.xlsx")
@login_required
def export_orb():
    """Export the current history buffer as an Excel file."""
    try:
        import openpyxl
    except ImportError:
        return err("openpyxl not installed", 500)

    packets = _service().get_history()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orbit Data"

    cols = [
        "server_ts", "seq", "latitude", "longitude", "true_altitude_m",
        "azimuth", "elevation", "slant_km", "range_rate_km_s", "doppler_100mhz",
        "bus_voltage", "battery_pct", "temperature", "gps_lock",
    ]
    ws.append(cols)
    for pkt in packets:
        ws.append([pkt.get(c) for c in cols])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="tansat_orbit.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
