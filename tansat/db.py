import re
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

_pool = None

def init_db(database_url: str) -> None:
    """Initialize connection pool, create tables, insert default settings."""
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=database_url)
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Create users table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(64) UNIQUE NOT NULL,
                email VARCHAR(255),
                full_name VARCHAR(120),
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(16) NOT NULL CHECK (role IN ('admin','operator','viewer')),
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            # Migration for DBs created before full_name existed (no-op when
            # the column is already present).
            cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(120);
            """)
            # Create audit_log table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                actor_username VARCHAR(64),
                action VARCHAR(128) NOT NULL,
                target VARCHAR(255),
                source_ip VARCHAR(45),
                detail TEXT
            );
            """)
            # Create station_settings table
            cur.execute("""
            CREATE TABLE IF NOT EXISTS station_settings (
                key VARCHAR(64) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            
            # Default settings
            defaults = [
                ('gs_lat', '-6.813737'),
                ('gs_lon', '39.280427'),
                ('gs_alt', '30'),
                ('baud_rate', '9600'),
                ('altitude_offset', '0'),
                ('attitude_alpha', '0.98'),
                ('gyro_scale', '1'),
                ('control_lock_ttl', '90'),
                ('station_name', 'Ground Station'),
                ('station_location', 'Dar es Salaam, Tanzania'),
            ]
            for key, value in defaults:
                cur.execute("""
                INSERT INTO station_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING;
                """, (key, value))

            # ── Orbital tracking subsystem tables ─────────────────────
            # satellites: the tracked spacecraft registry.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS satellites (
                id SERIAL PRIMARY KEY,
                norad_id INTEGER UNIQUE NOT NULL,
                name VARCHAR(120),
                callsign VARCHAR(32),
                is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            # tles: append-only element-set history.  A new TLE supersedes
            # the active one (status flip), never deletes — the full history
            # enables TLE-age vs error studies after the fact.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS tles (
                id SERIAL PRIMARY KEY,
                satellite_id INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
                norad_id INTEGER NOT NULL,
                line1 VARCHAR(80) NOT NULL,
                line2 VARCHAR(80) NOT NULL,
                epoch TIMESTAMPTZ NOT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'manual',
                retrieved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum_ok BOOLEAN NOT NULL DEFAULT TRUE,
                status VARCHAR(16) NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','superseded','rejected'))
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tles_sat_active
                ON tles (satellite_id, status);
            """)
            # position_fixes: observed positions (GPS telemetry or ORB feed).
            # raw_lat/raw_lon keep the PRE-filter values so the EMA smoothing
            # never destroys scientific data.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS position_fixes (
                id SERIAL PRIMARY KEY,
                satellite_id INTEGER REFERENCES satellites(id) ON DELETE CASCADE,
                fix_time TIMESTAMPTZ NOT NULL,
                server_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source VARCHAR(8) NOT NULL CHECK (source IN ('gps','orb')),
                lat DOUBLE PRECISION NOT NULL,
                lon DOUBLE PRECISION NOT NULL,
                alt_m DOUBLE PRECISION,
                raw_lat DOUBLE PRECISION,
                raw_lon DOUBLE PRECISION,
                az DOUBLE PRECISION,
                el DOUBLE PRECISION,
                slant_km DOUBLE PRECISION,
                range_rate_km_s DOUBLE PRECISION,
                doppler_100mhz DOUBLE PRECISION,
                rssi DOUBLE PRECISION,
                snr DOUBLE PRECISION
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_fixes_sat_time
                ON position_fixes (satellite_id, fix_time);
            """)
            # pass_predictions: cached SGP4 pass schedule per TLE.
            cur.execute("""
            CREATE TABLE IF NOT EXISTS pass_predictions (
                id SERIAL PRIMARY KEY,
                satellite_id INTEGER NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
                tle_id INTEGER REFERENCES tles(id) ON DELETE CASCADE,
                aos TIMESTAMPTZ NOT NULL,
                tca TIMESTAMPTZ,
                los TIMESTAMPTZ,
                max_elevation DOUBLE PRECISION,
                aos_azimuth DOUBLE PRECISION,
                los_azimuth DOUBLE PRECISION,
                duration_s DOUBLE PRECISION,
                min_elevation_mask DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_passpred_sat_aos
                ON pass_predictions (satellite_id, aos);
            """)
            # Tracking-related operator settings (defaults; admin-editable)
            tracking_defaults = [
                ('min_elevation_deg', '0'),
                ('prediction_horizon_h', '48'),
                ('tle_stale_hours', '72'),
                ('tle_max_age_hours', '336'),
            ]
            for key, value in tracking_defaults:
                cur.execute("""
                INSERT INTO station_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING;
                """, (key, value))
        conn.commit()

@contextmanager
def get_conn():
    """Get a connection from the pool (context manager friendly)."""
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db first.")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)

def validate_username(username: str) -> None:
    """Raises ValueError if invalid."""
    if not (3 <= len(username) <= 64):
        raise ValueError("Username must be between 3 and 64 characters.")
    if not re.match(r'^[a-zA-Z0-9._-]+$', username):
        raise ValueError("Username can only contain alphanumeric characters, dots, underscores, and hyphens.")

def count_active_admins() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = TRUE;")
            return cur.fetchone()[0]

def create_user(username, email, password_hash, role, must_change_password=True) -> dict:
    validate_username(username)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(%s);", (username,))
            if cur.fetchone():
                raise ValueError("Username already exists.")
            
            if email:
                cur.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(%s);", (email,))
                if cur.fetchone():
                    raise ValueError("Email already exists.")
            
            cur.execute("""
            INSERT INTO users (username, email, password_hash, role, must_change_password)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *;
            """, (username, email, password_hash, role, must_change_password))
            user = cur.fetchone()
        conn.commit()
    return dict(user)

def get_user_by_id(user_id) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
            user = cur.fetchone()
            return dict(user) if user else None

def get_user_by_username(username) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s);", (username,))
            user = cur.fetchone()
            return dict(user) if user else None

def get_user_by_email(email) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(%s);", (email,))
            user = cur.fetchone()
            return dict(user) if user else None

def list_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users ORDER BY id;")
            users = cur.fetchall()
            return [dict(u) for u in users]

def update_user(user_id, acting_user_id=None, **fields) -> dict:
    if not fields:
        return get_user_by_id(user_id)
        
    user = get_user_by_id(user_id)
    if not user:
        raise ValueError("User not found.")
        
    if 'username' in fields:
        validate_username(fields['username'])
        existing = get_user_by_username(fields['username'])
        if existing and existing['id'] != user_id:
            raise ValueError("Username already exists.")
            
    if 'email' in fields and fields['email']:
        existing = get_user_by_email(fields['email'])
        if existing and existing['id'] != user_id:
            raise ValueError("Email already exists.")
            
    if acting_user_id and acting_user_id == user_id:
        if 'is_active' in fields and not fields['is_active']:
            raise ValueError("Cannot suspend own account.")
            
    if user['role'] == 'admin' and user['is_active']:
        is_demoting = 'role' in fields and fields['role'] != 'admin'
        is_suspending = 'is_active' in fields and not fields['is_active']
        if is_demoting or is_suspending:
            if count_active_admins() <= 1:
                raise ValueError("Cannot demote or suspend the last active admin.")

    set_clauses = []
    values = []
    for k, v in fields.items():
        set_clauses.append(f"{k} = %s")
        values.append(v)
    
    set_clauses.append("updated_at = NOW()")
    
    query = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = %s RETURNING *;"
    values.append(user_id)
    
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, tuple(values))
            updated_user = cur.fetchone()
        conn.commit()
    return dict(updated_user)

def delete_user(user_id, acting_user_id=None) -> bool:
    if acting_user_id and acting_user_id == user_id:
        raise ValueError("Cannot delete own account.")
        
    user = get_user_by_id(user_id)
    if not user:
        return False
        
    if user['role'] == 'admin' and user['is_active']:
        if count_active_admins() <= 1:
            raise ValueError("Cannot delete the last active admin.")
            
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s;", (user_id,))
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted

def audit(actor_username, action, target=None, source_ip=None, detail=None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO audit_log (actor_username, action, target, source_ip, detail)
            VALUES (%s, %s, %s, %s, %s);
            """, (actor_username, action, target, source_ip, detail))
        conn.commit()

def get_audit_log(limit=200, offset=0) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT * FROM audit_log
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s;
            """, (limit, offset))
            return [dict(row) for row in cur.fetchall()]

def get_setting(key: str, default=None) -> str | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM station_settings WHERE key = %s;", (key,))
            row = cur.fetchone()
            return row[0] if row else default

def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO station_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
            """, (key, value))
        conn.commit()

def get_all_settings() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT key, value FROM station_settings;")
            return {row['key']: row['value'] for row in cur.fetchall()}
