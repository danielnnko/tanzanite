# TANZANITESAT Ground Station Web Application

A full-stack, multi-operator CubeSat ground station web application for LEO satellite operations.

---

## Quick Start

### 1. Requirements

- Python 3.11+
- PostgreSQL 14+
- Windows / Linux / macOS

### 2. Install dependencies

```bash
cd web-v3
pip install -r requirements.txt
```

### 3. Create the PostgreSQL database

```bash
# Windows PowerShell
psql -U postgres -c "CREATE DATABASE tansat;"

# Or with a specific user
psql -U postgres -c "CREATE USER tansat_user WITH PASSWORD 'yourpassword';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE tansat TO tansat_user;"
```

### 4. Set environment variables

```powershell
# Windows PowerShell
$env:DATABASE_URL = "postgresql://tansat_user:yourpassword@localhost/tansat"
$env:SECRET_KEY   = "your-secret-key-min-32-chars-random"
$env:DATA_DIR     = "data"      # optional, default: data/
$env:PORT         = "5000"      # optional, default: 5000
```

```bash
# Linux / macOS
export DATABASE_URL="postgresql://tansat_user:yourpassword@localhost/tansat"
export SECRET_KEY="your-secret-key-min-32-chars-random"
```

### 5. Run

```bash
python run.py
```

On first run, an `admin` bootstrap account is created and the **temporary password is printed to the console**. Change it immediately after first login.

Open: http://localhost:5000

---

## Architecture

```
web-v3/
├── tansat/              # Framework-free domain core (no Flask imports)
│   ├── parser.py        # Wire-protocol parser (§2 — exact dispatch order)
│   ├── physics.py       # Physics algorithms (§3 — exact formulas)
│   ├── acquisition.py   # Serial reader + SSE fan-out + control lock
│   ├── db.py            # PostgreSQL schema + CRUD (psycopg2)
│   └── auth.py          # bcrypt + Flask-Login + role decorators
├── app/
│   ├── __init__.py      # Flask app factory
│   ├── routes/
│   │   ├── auth.py      # /login /logout /change-password
│   │   ├── pages.py     # HTML page routes (12 pages)
│   │   ├── api.py       # /api/* (SSE, control lock, acquisition, uplink)
│   │   └── admin.py     # /admin/* (settings, accounts, audit, OTA)
│   ├── templates/       # Jinja2 HTML templates
│   └── static/
│       ├── css/style.css   # Full design system (§4)
│       ├── js/stream.js    # Shared SSE client
│       └── js/charts.js    # Chart.js wrappers (exact series colors)
├── tests/
│   ├── test_parser.py
│   └── test_physics.py
├── data/                # CSV session logs + payload images (auto-created)
├── requirements.txt
├── run.py               # Entry point
└── README.md
```

---

## Running Tests

```bash
cd web-v3
python -m pytest tests/ -v
```

---

## Pages

| URL | Page | Access |
|---|---|---|
| `/` | Dashboard | All |
| `/telemetry` | Full Telemetry | All |
| `/logs` | Analytics Logs | All |
| `/map` | GIS Map View | All |
| `/tracking` | Satellite Tracking | All |
| `/attitude` | 3D Attitude View | All |
| `/earth-orbit` | **Earth Orbit View** (3D, §8) | All |
| `/payload` | Payload Feed | All |
| `/admin/settings` | System Settings | Admin |
| `/admin/nodes` | Node Manager | Admin |
| `/admin/firmware` | Firmware OTA | Admin |
| `/admin/accounts` | Operator Accounts | Admin |
| `/admin/audit` | Audit Trail | Admin |

---

## API Endpoints

| Method | URL | Description |
|---|---|---|
| GET | `/api/stream` | Server-Sent Events (live telemetry) |
| GET | `/api/status` | Status snapshot JSON |
| GET | `/api/history?limit=N` | Chart back-fill (last N packets) |
| GET | `/api/ports` | Serial ports visible to **server** |
| POST | `/api/control/take` | Acquire control lock |
| POST | `/api/control/release` | Release control lock |
| POST | `/api/control/heartbeat` | Refresh 90s TTL |
| POST | `/api/acquisition/start` | Start serial reader |
| POST | `/api/acquisition/stop` | Stop serial reader |
| POST | `/api/uplink` | Send uplink command |
| GET | `/api/logs` | List CSV session files |
| GET | `/api/logs/<name>` | Download CSV session |
| GET | `/api/logs/<name>.preview` | Preview first 50 lines |
| GET | `/api/export/orb.xlsx` | Orbit data Excel export |

---

## Roles

| Role | Can view | Can take control / connect | Force-seize lock | Admin pages |
|---|---|---|---|---|
| Admin | ✓ | ✓ | ✓ | ✓ |
| Operator | ✓ | ✓ | ✗ | ✗ |
| Viewer | ✓ | ✗ | ✗ | ✗ |

---

## Ground Station Configuration

Default (configurable in admin settings):
- **GS_LAT**: -6.813737
- **GS_LON**: 39.280427
- **GS_ALT**: 30 m

---

## Production Deployment

> ⚠️ **CRITICAL**: Run as a **single process** (not multiple workers).
> The acquisition service and control lock are per-process singletons.
> Multiple gunicorn workers would each think they own the radio independently.

```bash
# Correct — single process, multiple threads
gunicorn -w 1 --threads 16 -b 0.0.0.0:5000 "app:create_app()"

# WRONG — never do this
gunicorn -w 4 "app:create_app()"   # ← 4 workers = 4 radio readers
```

### Nginx reverse proxy (required for SSE)

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;

    # Critical for SSE: disable buffering
    proxy_buffering off;
    proxy_cache off;
    proxy_set_header Connection '';
    proxy_http_version 1.1;
    chunked_transfer_encoding on;
}
```

---

## Wire Protocol

The modem emits newline-delimited text. The parser handles:

1. RSSI/SNR tag stripping (both `RSSI:-82dBm SNR:9.1dB` and `[RSSI=-82 SNR=9.1]` formats)
2. 14-field bare comma-separated TLM (primary format)
3. JSON packets (dispatched on `type` field)
4. Tagged CSV: `HK`, `ORB`, `IMG`, `BEACON`

Uplink commands are framed with NMEA-style XOR checksum + CRLF:
`PING` → `PING*4D\r\n`
