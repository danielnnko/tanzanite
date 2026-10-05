"""
tansat/orbit/tles.py
────────────────────
TLE validation, epoch parsing, staleness policy and persistence.

A TLE (Two-Line Element set) is the *input currency* of SGP4: a snapshot of
mean orbital elements at an epoch.  It is a MODEL INPUT, not an observation —
therefore it lives in its own append-only table, separate from telemetry and
from position fixes (which ARE observations).  Reasoning: architecture §13.

Staleness policy (architecture §11).  CubeSats have a high area-to-mass
ratio, so drag-induced element drift is faster than for a typical LEO
spacecraft — the thresholds below are deliberately conservative:

    age <  24 h    FRESH      full confidence
    24 h – 72 h    AGING      usable, flagged in the UI
    72 h – 7 d     STALE      usable with warning; pass prediction degraded
    7 d  – 14 d    DEAD_ZONE  predicted track drawn dimmed
    >  14 d        EXPIRED    predicted track withdrawn; GPS/ORB sources only
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from tansat import db as tansat_db

logger = logging.getLogger(__name__)

# ── Staleness thresholds (hours) ────────────────────────────────────
TLE_FRESH_H  = 24.0
TLE_AGING_H  = 72.0
TLE_STALE_H  = 24.0 * 7.0
TLE_EXPIRY_H = 24.0 * 14.0

# Position-source freshness windows (seconds).  GPS fixes remain the
# authoritative source for a full pass-length window after the last fix;
# ORB packets are a propagator feed whose value decays much faster.
GPS_FRESH_S = 300.0
ORB_FRESH_S = 120.0


class TleError(ValueError):
    """Raised on structurally invalid TLE text."""


# ── Checksum ─────────────────────────────────────────────────────────
def tle_checksum(line: str) -> int:
    """TLE checksum: sum of all digits in columns 1–68 (minus signs count 1),
    mod 10.  Column 69 (the checksum character itself) is EXCLUDED.

    It catches copy/paste mangling and column shifts but is NOT cryptographic —
    a wrong-but-consistent TLE passes, so element sanity checks matter too.
    """
    body = line[:68] if len(line) >= 69 else line
    s = 0
    for ch in body:
        if ch.isdigit():
            s += int(ch)
        elif ch == "-":
            s += 1
    return s % 10


# ── Epoch ────────────────────────────────────────────────────────────
def parse_epoch(line1: str) -> datetime:
    """TLE line-1 columns 19–32 (1-indexed): YYDDD.DDDDDDDD → aware UTC datetime.

    Two-digit year pivot at 57 (the space age began 1957-10-04).
    """
    yy = int(line1[18:20])
    doy = float(line1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1.0)


# ── Element sanity limits (catch garbage that passes the checksum) ───
_MM_MIN, _MM_MAX = 8.0, 20.0          # revs/day → 72–180 min period (LEO band)
_INC_MAX = 145.0                      # degrees; nonsense above this for our sats
_ECC_MAX = 0.10                       # TLE mean eccentricity for LEO
_BSTAR_ABS_MAX = 1e-2


def parse_tle(text: str) -> dict:
    """Parse + validate a 2-line or 3-line TLE block.

    Accepts the three-line form ("0 NAME" header), the bare two-line form,
    CRLF line endings and arbitrary surrounding whitespace.  Checksums are
    validated strictly.

    Returns a dict with line1/line2, satnum, classification, epoch and the
    mean elements.  Raises TleError with a human-readable reason.
    """
    if not text or not text.strip():
        raise TleError("Empty TLE text")

    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln]

    if len(lines) >= 3 and lines[-2].startswith("1 ") and lines[-1].startswith("2 "):
        name, l1, l2 = lines[-3], lines[-2], lines[-1]
    elif len(lines) >= 2 and lines[-2].startswith("1 ") and lines[-1].startswith("2 "):
        name, l1, l2 = None, lines[-2], lines[-1]
    else:
        raise TleError("Expected '1 NNNN…' and '2 NNNN…' lines (optionally after a name line)")

    # Normalise width: TLE lines are exactly 69 columns; tolerate truncation
    # of trailing spaces but reject lines that are too short to parse.
    if len(l1) < 68 or len(l2) < 68:
        raise TleError("TLE lines too short (expected 69 columns)")

    if tle_checksum(l1) != int(l1[68]):
        raise TleError("Line-1 checksum mismatch — text was mangled in transit")
    if tle_checksum(l2) != int(l2[68]):
        raise TleError("Line-2 checksum mismatch — text was mangled in transit")

    satnum_str = l1[2:7].strip()
    if not satnum_str.isdigit():
        raise TleError(f"Invalid satellite number '{satnum_str}'")
    satnum = int(satnum_str)
    if l2[2:7].strip() != satnum_str:
        raise TleError("Satellite number differs between line 1 and line 2")

    try:
        epoch = parse_epoch(l1)
    except (ValueError, IndexError) as e:
        raise TleError(f"Unparseable epoch field: {e}") from e

    if epoch > datetime.now(timezone.utc) + timedelta(days=1):
        raise TleError("TLE epoch is in the future — wrong data")

    inc  = float(l2[8:16])
    raan = float(l2[17:25])
    ecc  = float("0." + l2[26:33].strip())
    argp = float(l2[34:42])
    ma   = float(l2[43:51])
    mm   = float(l2[52:63])
    # B* is columns 54–61 (1-indexed), an exponential like " 12345-4" or " 00000-0"
    bstar = _parse_exponential(l1[53:61])

    if not (0.0 <= inc <= 180.0):
        raise TleError(f"Inclination {inc}° out of range")
    if inc > _INC_MAX:
        logger.info("TLE inclination %.1f° unusual for this program's satellites", inc)
    if not (0.0 <= ecc <= _ECC_MAX):
        raise TleError(f"Eccentricity {ecc} out of LEO range")
    if not (_MM_MIN <= mm <= _MM_MAX):
        raise TleError(
            f"Mean motion {mm} rev/day outside LEO band ({_MM_MIN}–{_MM_MAX}); "
            "refusing to track non-LEO objects with this pipeline")
    if abs(bstar) > _BSTAR_ABS_MAX:
        logger.info("TLE B* %.3e unusually large — heavy drag or parse issue", bstar)

    return {
        "line1": l1,
        "line2": l2,
        "satnum": satnum,
        "name": name,
        "classification": l1[7],
        "launch_piece": l1[9:11].strip(),
        "epoch": epoch,
        "inclination_deg": inc,
        "raan_deg": raan,
        "eccentricity": ecc,
        "argp_deg": argp,
        "mean_anomaly_deg": ma,
        "mean_motion_revs_per_day": mm,
        "period_minutes": (1440.0 / mm) if mm > 0 else None,
        "bstar": bstar,
    }


def _parse_exponential(field: str) -> float:
    """Parse a TLE exponential field like ' 12345-4' → 0.12345e-4.

    Convention: an assumed leading '0.' and the exponent as sign+digits at
    the end.  ' 00000-0' → 0.0.  Blank/zero field → 0.0.
    """
    f = field.strip()
    if not f:
        return 0.0
    m = re.fullmatch(r"([ +-]?)(\d{5})([+-]\d)", f)
    if not m:
        try:
            return float(f)
        except ValueError:
            return 0.0
    mant = float("0." + m.group(2))
    exp = int(m.group(3))
    val = mant * (10.0 ** exp)
    return -val if m.group(1) == "-" else val


# ── Staleness policy ─────────────────────────────────────────────────
def tle_age_hours(epoch: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (now - epoch).total_seconds() / 3600.0


def tle_status(epoch: datetime, now: datetime | None = None) -> str:
    """Classify a TLE: fresh | aging | stale | dead_zone | expired."""
    age = tle_age_hours(epoch, now)
    if age < TLE_FRESH_H:
        return "fresh"
    if age < TLE_AGING_H:
        return "aging"
    if age < TLE_STALE_H:
        return "stale"
    if age < TLE_EXPIRY_H:
        return "dead_zone"
    return "expired"


def tle_usable(epoch: datetime, now: datetime | None = None) -> bool:
    return tle_status(epoch, now) != "expired"


# ── Persistence ──────────────────────────────────────────────────────
def ensure_satellite(norad_id: int, name: str | None = None,
                     callsign: str | None = None) -> int:
    """Get or create the satellite row for a NORAD id.  Returns satellite id."""
    with tansat_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM satellites WHERE norad_id = %s;", (norad_id,))
            row = cur.fetchone()
            if row:
                if name:
                    cur.execute(
                        "UPDATE satellites SET name = %s "
                        "WHERE norad_id = %s AND (name IS NULL OR name = '');",
                        (name, norad_id))
                conn.commit()
                return row[0]
            cur.execute(
                "INSERT INTO satellites (norad_id, name, callsign, is_primary) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id;",
                (norad_id, name, callsign))
            sid = cur.fetchone()[0]
        conn.commit()
    return sid


def get_primary_satellite() -> dict | None:
    """The tracked satellite (is_primary), or None if not configured yet."""
    with tansat_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, norad_id, name, callsign FROM satellites "
                "WHERE is_primary = TRUE ORDER BY id LIMIT 1;")
            row = cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "norad_id": row[1],
                    "name": row[2], "callsign": row[3]}


def import_tle(text: str, source: str, actor: str | None = None) -> dict:
    """Validate + store a TLE, superseding the currently active one.

    Append-only design: the old active row is flipped to 'superseded', never
    deleted — full element history is retained so TLE-age vs error can be
    studied after the fact (architecture §38).

    Returns the parsed TLE dict plus its database id.
    """
    parsed = parse_tle(text)   # raises TleError on bad input
    now = datetime.now(timezone.utc)

    with tansat_db.get_conn() as conn:
        with conn.cursor() as cur:
            sid = None
            cur.execute("SELECT id FROM satellites WHERE norad_id = %s;",
                        (parsed["satnum"],))
            row = cur.fetchone()
            if row:
                sid = row[0]
                if parsed["name"]:
                    cur.execute("UPDATE satellites SET name = %s "
                                "WHERE id = %s AND (name IS NULL OR name = '');",
                                (parsed["name"], sid))
            else:
                cur.execute(
                    "INSERT INTO satellites (norad_id, name, is_primary) "
                    "SELECT %s, %s, NOT EXISTS (SELECT 1 FROM satellites WHERE is_primary) "
                    "RETURNING id;",
                    (parsed["satnum"], parsed["name"]))
                sid = cur.fetchone()[0]

            # Epoch-duplicate guard: re-importing the identical element set
            # must not create a duplicate history row.
            cur.execute(
                "SELECT id FROM tles WHERE satellite_id = %s AND epoch = %s "
                "AND line1 = %s AND line2 = %s;",
                (sid, parsed["epoch"], parsed["line1"], parsed["line2"]))
            dup = cur.fetchone()
            if dup:
                conn.commit()
                parsed["id"] = dup[0]
                parsed["duplicate"] = True
                return parsed

            cur.execute("UPDATE tles SET status = 'superseded' "
                        "WHERE satellite_id = %s AND status = 'active';", (sid,))
            cur.execute(
                "INSERT INTO tles (satellite_id, norad_id, line1, line2, epoch, "
                "source, retrieved_at, checksum_ok, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'active') RETURNING id;",
                (sid, parsed["satnum"], parsed["line1"], parsed["line2"],
                 parsed["epoch"], source, now))
            parsed["id"] = cur.fetchone()[0]
            parsed["duplicate"] = False
        conn.commit()

    if actor:
        tansat_db.audit(
            actor_username=actor,
            action="tle_import",
            target=f"norad:{parsed['satnum']}",
            detail=f"source={source} epoch={parsed['epoch'].isoformat()}",
        )
    logger.info("TLE imported: NORAD %s epoch %s (source=%s)",
                parsed["satnum"], parsed["epoch"].isoformat(), source)
    return parsed


def get_active_tle(satellite_id: int | None = None) -> dict | None:
    """The active TLE row (parsed fields included) or None."""
    with tansat_db.get_conn() as conn:
        with conn.cursor() as cur:
            if satellite_id is None:
                cur.execute(
                    "SELECT t.id, t.satellite_id, t.norad_id, t.line1, t.line2, "
                    "t.epoch, t.source, t.retrieved_at, s.name "
                    "FROM tles t JOIN satellites s ON s.id = t.satellite_id "
                    "WHERE t.status = 'active' "
                    "ORDER BY t.epoch DESC LIMIT 1;")
            else:
                cur.execute(
                    "SELECT t.id, t.satellite_id, t.norad_id, t.line1, t.line2, "
                    "t.epoch, t.source, t.retrieved_at, s.name "
                    "FROM tles t JOIN satellites s ON s.id = t.satellite_id "
                    "WHERE t.status = 'active' AND t.satellite_id = %s "
                    "ORDER BY t.epoch DESC LIMIT 1;",
                    (satellite_id,))
            row = cur.fetchone()
            if not row:
                return None
            keys = ("id", "satellite_id", "norad_id", "line1", "line2",
                    "epoch", "source", "retrieved_at", "name")
            rec = dict(zip(keys, row))
    try:
        rec["elements"] = parse_tle(rec["line1"] + "\n" + rec["line2"])
    except TleError:
        rec["elements"] = None
    rec["age_h"] = tle_age_hours(rec["epoch"])
    rec["status"] = tle_status(rec["epoch"])
    return rec


def list_tles(satellite_id: int, limit: int = 50) -> list[dict]:
    with tansat_db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, epoch, source, retrieved_at, status, line1, line2 "
                "FROM tles WHERE satellite_id = %s "
                "ORDER BY epoch DESC LIMIT %s;", (satellite_id, limit))
            return [
                {"id": r[0], "epoch": r[1], "source": r[2], "retrieved_at": r[3],
                 "status": r[4], "line1": r[5], "line2": r[6]}
                for r in cur.fetchall()
            ]
