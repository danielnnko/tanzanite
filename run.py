"""
run.py — TANSAT ground station entry point.

Usage:
    python run.py

Environment variables:
    DATABASE_URL   PostgreSQL DSN  (required)
                   e.g. postgresql://user:pass@localhost/tansat
    SECRET_KEY     Flask secret key (required in production)
    DATA_DIR       Path for CSV logs and payload images (default: data/)
    PORT           HTTP port (default: 5000)
    HOST           Bind host (default: 0.0.0.0)

Important: The server MUST run as a single process (threaded=True, NOT multiple workers).
The acquisition service and control lock are per-process singletons. Multiple workers
would each think they own the radio independently.
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── Load .env file if present ───────────────────────────────────────
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v

# ── Verify required environment variables ──────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print(
        "\nERROR: DATABASE_URL environment variable is not set.\n"
        "Example: export DATABASE_URL=postgresql://user:pass@localhost/tansat\n",
        file=sys.stderr,
    )
    sys.exit(1)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    logging.warning(
        "SECRET_KEY is not set. A random key will be generated — "
        "all sessions will be invalidated on restart. "
        "Set SECRET_KEY in production!"
    )

# ── Create and run the Flask app ───────────────────────────────────
from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))

    logging.info("Starting TANSAT ground station on http://%s:%d", host, port)
    logging.info(
        "IMPORTANT: Running in single-process threaded mode. "
        "Do NOT use gunicorn with multiple workers."
    )

    # threaded=True is REQUIRED for SSE + serial reader coexistence.
    # use_reloader=False prevents the acquisition thread from being duplicated.
    app.run(
        host=host,
        port=port,
        threaded=True,
        use_reloader=False,
        debug=os.environ.get("DEBUG", "0") == "1",
    )
