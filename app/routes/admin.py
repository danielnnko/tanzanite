"""
app/routes/admin.py
───────────────────
Admin-only routes:
  /admin/settings  — ground station + calibration settings
  /admin/nodes     — node manager + uplink console
  /admin/firmware  — firmware OTA upload
  /admin/accounts  — operator account management (all 8 guard rails)
  /admin/audit     — audit trail browser
"""

import os
from pathlib import Path

from flask import (Blueprint, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from tansat import auth as tansat_auth
from tansat import db as tansat_db
from tansat import acquisition as acq_svc
from tansat.auth import require_role

admin_bp = Blueprint("admin", __name__)


def _svc():
    return acq_svc.get_service()


# ── System Settings ────────────────────────────────────────────────
@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@require_role("admin")
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "save_gs":
            keys = ["gs_lat", "gs_lon", "gs_alt", "baud_rate"]
            for k in keys:
                val = request.form.get(k, "").strip()
                if val:
                    tansat_db.set_setting(k, val)
            tansat_db.audit(
                actor_username=current_user.username,
                action="settings_update_gs",
                source_ip=request.remote_addr,
                detail=str({k: request.form.get(k) for k in keys}),
            )
            # Sync new GS config into acquisition service
            s = tansat_db.get_all_settings()
            try:
                _svc().update_gs_config(
                    gs_lat=float(s.get("gs_lat", -6.813737)),
                    gs_lon=float(s.get("gs_lon", 39.280427)),
                    gs_alt=float(s.get("gs_alt", 30.0)),
                    alt_offset=float(s.get("altitude_offset", 0.0)),
                )
            except Exception:
                pass
            flash("Ground station settings saved.", "success")

        elif action == "save_attitude":
            keys = ["attitude_alpha", "gyro_scale"]
            values = {}
            parse_err = None
            try:
                values["attitude_alpha"] = float(request.form.get("attitude_alpha", ""))
                values["gyro_scale"]   = float(request.form.get("gyro_scale", ""))
            except ValueError:
                parse_err = "Alpha and gyro scale must be numeric."
            if parse_err is None and not (0.0 <= values["attitude_alpha"] <= 1.0):
                parse_err = "Attitude alpha must be between 0 and 1."
            if parse_err is None and values["gyro_scale"] <= 0:
                parse_err = "Gyro scale must be positive."
            if parse_err:
                flash(parse_err, "warning")
            else:
                for k in keys:
                    tansat_db.set_setting(k, str(values[k]))
                tansat_db.audit(
                    actor_username=current_user.username,
                    action="settings_update_attitude",
                    source_ip=request.remote_addr,
                    detail=str(values),
                )
                # Push live into the running acquisition service
                try:
                    _svc().update_attitude_config(
                        alpha=values["attitude_alpha"],
                        gyro_scale=values["gyro_scale"],
                    )
                except Exception:
                    pass
                flash(
                    f"Attitude tuning saved (alpha={values['attitude_alpha']:g}, "
                    f"gyro scale={values['gyro_scale']:g}).",
                    "success",
                )
                if values["gyro_scale"] > 10:
                    flash(
                        "Warning: large gyro scale. With the satellite stationary, "
                        "any gyro bias is multiplied by this factor and the filter "
                        "integrates it into continuous rotation (runaway). Confirm "
                        "the ω readouts show realistic °/s while rotating, and that "
                        "pitch/roll stay bounded while stationary.",
                        "warning",
                    )

        elif action == "zero_altitude":
            # Capture current raw altitude_m from last packet as offset
            history = _svc().get_history(limit=1)
            if history:
                raw_alt = history[-1].get("altitude_m", 0.0) or 0.0
                tansat_db.set_setting("altitude_offset", str(raw_alt))
                s = tansat_db.get_all_settings()
                try:
                    _svc().update_gs_config(
                        gs_lat=float(s.get("gs_lat", -6.813737)),
                        gs_lon=float(s.get("gs_lon", 39.280427)),
                        gs_alt=float(s.get("gs_alt", 30.0)),
                        alt_offset=raw_alt,
                    )
                except Exception:
                    pass
                tansat_db.audit(
                    actor_username=current_user.username,
                    action="altitude_zero",
                    source_ip=request.remote_addr,
                    detail=f"offset={raw_alt}",
                )
                flash(f"Altitude zeroed (offset = {raw_alt:.2f} m).", "success")
            else:
                flash("No telemetry packets received yet — cannot zero altitude.", "warning")

        elif action == "save_lock_ttl":
            val = request.form.get("lock_ttl", "").strip()
            if val:
                try:
                    ttl = int(val)
                    if ttl > 0:
                        tansat_db.set_setting("control_lock_ttl", str(ttl))
                        tansat_db.audit(
                            actor_username=current_user.username,
                            action="settings_update_lock_ttl",
                            source_ip=request.remote_addr,
                            detail=f"control_lock_ttl={ttl}",
                        )
                        flash("Control lock TTL saved.", "success")
                except ValueError:
                    flash("Lock TTL must be a whole number of seconds.", "danger")

        elif action == "save_station_info":
            for k in ("station_name", "station_location"):
                val = request.form.get(k, "").strip()
                if val:
                    tansat_db.set_setting(k, val)
            tansat_db.audit(
                actor_username=current_user.username,
                action="settings_update_station_info",
                source_ip=request.remote_addr,
            )
            flash("Ground station info saved.", "success")

        return redirect(url_for("admin.settings"))

    s = tansat_db.get_all_settings()
    return render_template("settings.html", settings=s)


# ── Node Manager ────────────────────────────────────────────────────
@admin_bp.route("/nodes", methods=["GET", "POST"])
@login_required
@require_role("admin", "operator")   # operators may run the antenna console; uplink still needs the control lock
def nodes():
    uplink_result = None
    if request.method == "POST":
        command = request.form.get("command", "").strip()
        if command:
            svc = _svc()
            if not svc.control_lock.is_held_by(current_user.username):
                flash("You must hold the control lock to send uplink commands.", "danger")
            else:
                success, msg = svc.send_uplink(command)
                uplink_result = {"success": success, "message": msg, "command": command}
                if success:
                    tansat_db.audit(
                        actor_username=current_user.username,
                        action="uplink_command",
                        source_ip=request.remote_addr,
                        detail=f"CMD: {command}",
                    )
    return render_template("node_manager.html", uplink_result=uplink_result, status=_svc().get_status())


# ── Firmware OTA ────────────────────────────────────────────────────
@admin_bp.route("/firmware", methods=["GET", "POST"])
@login_required
@require_role("admin")
def firmware():
    if request.method == "POST":
        if "firmware_file" not in request.files:
            flash("No file selected.", "danger")
            return redirect(url_for("admin.firmware"))
        f = request.files["firmware_file"]
        if not f.filename:
            flash("No file selected.", "danger")
            return redirect(url_for("admin.firmware"))

        # Save to data/firmware/
        fw_dir = Path(current_app.config.get("DATA_DIR", "data")) / "firmware"
        fw_dir.mkdir(parents=True, exist_ok=True)
        from werkzeug.utils import secure_filename
        filename = secure_filename(f.filename)
        dest = fw_dir / filename
        f.save(dest)
        tansat_db.audit(
            actor_username=current_user.username,
            action="firmware_upload",
            source_ip=request.remote_addr,
            detail=f"file={filename}, size={dest.stat().st_size}",
        )
        flash(f"Firmware '{filename}' uploaded successfully.", "success")
        return redirect(url_for("admin.firmware"))

    fw_dir = Path(current_app.config.get("DATA_DIR", "data")) / "firmware"
    files = []
    if fw_dir.exists():
        for f in sorted(fw_dir.iterdir(), reverse=True):
            if f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size})
    return render_template("firmware.html", firmware_files=files)


# ── Operator Accounts ──────────────────────────────────────────────
@admin_bp.route("/accounts", methods=["GET"])
@login_required
@require_role("admin")
def accounts():
    users = tansat_db.list_users()
    return render_template("accounts.html", users=users)


@admin_bp.route("/accounts/create", methods=["POST"])
@login_required
@require_role("admin")
def account_create():
    username  = request.form.get("username", "").strip()
    email     = request.form.get("email", "").strip() or None
    role      = request.form.get("role", "viewer")
    temp_pw   = tansat_auth.generate_temp_password()

    try:
        tansat_db.validate_username(username)
        if role not in ("admin", "operator", "viewer"):
            raise ValueError("Invalid role")
        pw_hash = tansat_auth.hash_password(temp_pw)
        tansat_db.create_user(username, email, pw_hash, role, must_change_password=True)
        tansat_db.audit(
            actor_username=current_user.username,
            action="account_create",
            target=username,
            source_ip=request.remote_addr,
            detail=f"role={role}",
        )
        flash(
            f"Account '{username}' created. Temporary password: {temp_pw} "
            f"(user must change on first login)",
            "success",
        )
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.accounts"))


@admin_bp.route("/accounts/<int:user_id>/suspend", methods=["POST"])
@login_required
@require_role("admin")
def account_suspend(user_id: int):
    try:
        row = tansat_db.get_user_by_id(user_id)
        if not row:
            flash("User not found.", "danger")
        else:
            tansat_db.update_user(user_id, is_active=False,
                                  acting_user_id=int(current_user.id))
            tansat_db.audit(
                actor_username=current_user.username,
                action="account_suspend",
                target=row["username"],
                source_ip=request.remote_addr,
            )
            flash(f"Account '{row['username']}' suspended.", "warning")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.accounts"))


@admin_bp.route("/accounts/<int:user_id>/reactivate", methods=["POST"])
@login_required
@require_role("admin")
def account_reactivate(user_id: int):
    row = tansat_db.get_user_by_id(user_id)
    if not row:
        flash("User not found.", "danger")
    else:
        tansat_db.update_user(user_id, is_active=True)
        tansat_db.audit(
            actor_username=current_user.username,
            action="account_reactivate",
            target=row["username"],
            source_ip=request.remote_addr,
        )
        flash(f"Account '{row['username']}' reactivated.", "success")
    return redirect(url_for("admin.accounts"))


@admin_bp.route("/accounts/<int:user_id>/delete", methods=["POST"])
@login_required
@require_role("admin")
def account_delete(user_id: int):
    try:
        row = tansat_db.get_user_by_id(user_id)
        if not row:
            flash("User not found.", "danger")
        else:
            username = row["username"]
            tansat_db.delete_user(user_id, acting_user_id=int(current_user.id))
            tansat_db.audit(
                actor_username=current_user.username,
                action="account_delete",
                target=username,
                source_ip=request.remote_addr,
            )
            flash(f"Account '{username}' deleted.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.accounts"))


@admin_bp.route("/accounts/<int:user_id>/reset-password", methods=["POST"])
@login_required
@require_role("admin")
def account_reset_password(user_id: int):
    row = tansat_db.get_user_by_id(user_id)
    if not row:
        flash("User not found.", "danger")
    else:
        temp_pw = tansat_auth.generate_temp_password()
        tansat_db.update_user(
            user_id,
            password_hash=tansat_auth.hash_password(temp_pw),
            must_change_password=True,
        )
        tansat_db.audit(
            actor_username=current_user.username,
            action="account_reset_password",
            target=row["username"],
            source_ip=request.remote_addr,
        )
        flash(
            f"Password for '{row['username']}' reset. New temp password: {temp_pw}",
            "success",
        )
    return redirect(url_for("admin.accounts"))


@admin_bp.route("/accounts/<int:user_id>/change-role", methods=["POST"])
@login_required
@require_role("admin")
def account_change_role(user_id: int):
    new_role = request.form.get("role", "viewer")
    if new_role not in ("admin", "operator", "viewer"):
        flash("Invalid role.", "danger")
        return redirect(url_for("admin.accounts"))
    try:
        row = tansat_db.get_user_by_id(user_id)
        if not row:
            flash("User not found.", "danger")
        else:
            tansat_db.update_user(user_id, role=new_role,
                                  acting_user_id=int(current_user.id))
            tansat_db.audit(
                actor_username=current_user.username,
                action="account_role_change",
                target=row["username"],
                source_ip=request.remote_addr,
                detail=f"new_role={new_role}",
            )
            flash(f"Role updated to '{new_role}' for '{row['username']}'.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("admin.accounts"))


# ── Audit Trail ────────────────────────────────────────────────────
@admin_bp.route("/audit")
@login_required
@require_role("admin")
def audit():
    limit  = request.args.get("limit", 200, type=int)
    offset = request.args.get("offset", 0, type=int)
    rows   = tansat_db.get_audit_log(limit=limit, offset=offset)
    return render_template("audit.html", rows=rows, limit=limit, offset=offset)
