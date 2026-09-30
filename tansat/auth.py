import bcrypt
from functools import wraps
from flask import session, redirect, url_for, abort, g
from flask_login import LoginManager, UserMixin, current_user, login_required
from tansat import db

login_manager = LoginManager()

class User(UserMixin):
    """Flask-Login user object wrapping a DB row dict."""
    def __init__(self, row: dict):
        self.id = str(row['id'])
        self.username = row['username']
        self.email = row.get('email')
        self.full_name = row.get('full_name')
        self.role = row['role']  # 'admin' | 'operator' | 'viewer'
        self._is_active = row['is_active']
        self.must_change_password = row['must_change_password']

    @property
    def is_active(self):
        return self._is_active

    def get_id(self):
        return self.id

@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return User(row) if row else None

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def generate_temp_password(length=12) -> str:
    """Generate a secure random alphanumeric temp password."""
    import secrets, string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def require_role(*roles):
    """Decorator that checks current_user.role is in roles. Must be used after @login_required."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if current_user.role not in roles:
                abort(403)
            # Forced password change is enforced globally (app/__init__.py
            # before_request hook) — no need to duplicate it here.
            return f(*args, **kwargs)
        return wrapped
    return decorator

def require_control(f):
    """Decorator: current user must hold the control lock."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        from tansat import acquisition as acq_svc
        if not acq_svc.get_service().control_lock.is_held_by(current_user.username):
            abort(403)
        return f(*args, **kwargs)
    return wrapped
