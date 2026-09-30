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


# ── Profile: personal details (own account only, any role) ──────────
@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Let any signed-in user edit their own personal details
    (full name, username, email). ID is display-only. Password is
    handled separately on the Account settings page."""
    row = tansat_db.get_user_by_id(int(current_user.id))
    if not row:
        flash("Session user no longer exists. Please sign in again.", "danger")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip() or None
        username  = (request.form.get("username") or "").strip()
        email     = (request.form.get("email") or "").strip() or None
        try:
            tansat_db.update_user(
                int(current_user.id),
                acting_user_id=int(current_user.id),
                full_name=full_name,
                username=username,
                email=email,
            )
        except ValueError as e:
            flash(str(e), "danger")
        else:
            # Username may have changed — refresh the session identity.
            fresh = tansat_db.get_user_by_id(int(current_user.id))
            login_user(tansat_auth.User(fresh), remember=False)
            tansat_db.audit(
                actor_username=current_user.username,
                action="profile_updated",
                target=f"user:{current_user.id}",
                source_ip=request.remote_addr,
                detail="User updated own profile details",
            )
            flash("Profile updated.", "success")
            return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html", user=row)

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
