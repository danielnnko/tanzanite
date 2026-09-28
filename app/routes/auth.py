"""
app/routes/auth.py
──────────────────
Login / logout / forced password change.
"""

from flask import (Blueprint, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from tansat import auth as tansat_auth
from tansat import db as tansat_db

auth_bp = Blueprint("auth", __name__)


# ── Login ──────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("pages.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        source_ip = request.remote_addr

        row = tansat_db.get_user_by_username(username)
        if row and row.get("is_active") and tansat_auth.check_password(password, row["password_hash"]):
            user = tansat_auth.User(row)
            login_user(user, remember=False)
            tansat_db.audit(
                actor_username=username,
                action="login",
                source_ip=source_ip,
                detail="Login successful",
            )
            if user.must_change_password:
                flash("Please change your password before continuing.", "warning")
                return redirect(url_for("auth.change_password"))
            return redirect(request.args.get("next") or url_for("pages.dashboard"))
        else:
            tansat_db.audit(
                actor_username=username,
                action="login_failed",
                source_ip=source_ip,
                detail="Invalid credentials or inactive account",
            )
            flash("Invalid username or password.", "danger")

    return render_template("auth/login.html")


# ── Logout ─────────────────────────────────────────────────────────
@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    tansat_db.audit(
        actor_username=current_user.username,
        action="logout",
        source_ip=request.remote_addr,
    )
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# ── Forced / voluntary password change ─────────────────────────────
@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw  = request.form.get("current_password", "")
        new_pw      = request.form.get("new_password", "")
        confirm_pw  = request.form.get("confirm_password", "")

        row = tansat_db.get_user_by_id(int(current_user.id))
        if not row or not tansat_auth.check_password(current_pw, row["password_hash"]):
            flash("Current password is incorrect.", "danger")
            return render_template("auth/change_password.html")

        if len(new_pw) < 8:
            flash("New password must be at least 8 characters.", "danger")
            return render_template("auth/change_password.html")

        if new_pw != confirm_pw:
            flash("Passwords do not match.", "danger")
            return render_template("auth/change_password.html")

        new_hash = tansat_auth.hash_password(new_pw)
        tansat_db.update_user(
            int(current_user.id),
            password_hash=new_hash,
            must_change_password=False,
        )
        tansat_db.audit(
            actor_username=current_user.username,
            action="password_changed",
            source_ip=request.remote_addr,
        )
        flash("Password changed successfully.", "success")
        return redirect(url_for("pages.dashboard"))

    return render_template("auth/change_password.html")
