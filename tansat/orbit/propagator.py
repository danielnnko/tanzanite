"""
tansat/orbit/propagator.py
──────────────────────────
SGP4 propagation wrapper.

Why SGP4 (architecture §14): it is THE standard analytic propagator for
catalogued Earth-orbiting objects, distributed by Vallado/CelesTrak.  It
consumes a TLE (mean elements at epoch: i, Ω, e, ω, M, n, B*) plus UTC and
produces position/velocity in the TEME frame.  Its force model covers

  • secular J2 (nodal regression ≈ −5°/day for our orbit, apsidal rotation)
  • short-period J2/J2²/J4 perturbations
  • Lane–Cranford atmospheric drag (static density model, driven by B*)

It deliberately does NOT model third-body gravity, solar radiation pressure,
or maneuvers — that is SDP4/numerical-propagator territory.  For a LEO
CubeSat (72–180 min period, e < 0.1) SGP4 with a TLE ≤ a few days old is the
industry-standard tracking input: ~1–3 km at epoch, growing ~1–3 km/day.

We use the `sgp4` package (Vallado's reference implementation, maintained on
PyPI) rather than re-implementing: an orbit propagator is the last place a
ground station should hand-roll numerics.  The `accelerated` build (compiled
extension) is used automatically when available.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
from sgp4.api import Satrec, jday

from tansat.orbit import coords


class PropagationError(RuntimeError):
    """SGP4 refused to propagate (bad elements, decayed orbit, …)."""


def _jday_utc(utc: datetime) -> tuple[float, float]:
    """(jd, fr) split required by the sgp4.api layer."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    jd, fr = jday(utc.year, utc.month, utc.day,
                  utc.hour, utc.minute, utc.second + utc.microsecond * 1e-6)
    return jd, fr


def make_satrec(line1: str, line2: str) -> Satrec:
    """Build a Satrec from TLE lines, raising PropagationError with context."""
    sat = Satrec.twoline2rv(line1, line2)
    if sat.error != 0:
        raise PropagationError(f"TLE load error code {sat.error}")
    return sat


def propagate(satrec: Satrec, utc: datetime) -> dict:
    """Single-epoch propagation.

    Returns dict with TEME position/velocity (km, km/s), geodetic subpoint
    (lat_deg, lon_deg, alt_m), ECEF position (m) and ECEF velocity (m/s).
    Raises PropagationError if SGP4 flags an error for this epoch.
    """
    jd, fr = _jday_utc(utc)
    e, r, v = satrec.sgp4(jd, fr)
    if e != 0:
        raise PropagationError(f"SGP4 error {e} at {utc.isoformat()}")
    r_teme_m = (r[0] * 1000.0, r[1] * 1000.0, r[2] * 1000.0)

    # TEME→ECEF for position; velocity rotated the same way then adjusted
    # for frame rotation: v_ecef = R·v_teme − ω×r_ecef
    th = math.radians(coords.gmst_deg(utc))
    c, s = math.cos(th), math.sin(th)
    x, y, z = r_teme_m
    r_ecef = (c * x + s * y, -s * x + c * y, z)
    vx, vy, vz = (v[0] * 1000.0, v[1] * 1000.0, v[2] * 1000.0)
    v_rot = (c * vx + s * vy, -s * vx + c * vy, vz)
    om = coords.EARTH_OMEGA
    v_ecef = (v_rot[0] + om * r_ecef[1],
              v_rot[1] - om * r_ecef[0],
              v_rot[2])

    lat, lon, alt = coords.geodetic_from_ecef(r_ecef)
    speed = math.sqrt(v_ecef[0] ** 2 + v_ecef[1] ** 2 + v_ecef[2] ** 2)
    return {
        "utc": utc,
        "r_teme_m": r_teme_m,
        "v_teme_mps": (vx, vy, vz),
        "r_ecef_m": r_ecef,
        "v_ecef_mps": v_ecef,
        "lat_deg": lat,
        "lon_deg": lon,
        "alt_m": alt,
        "speed_mps": speed,
    }


def propagate_batch(satrec: Satrec, times: list[datetime]) -> dict:
    """Vectorized multi-epoch propagation via the array API.

    ~50–100× faster than a Python loop of single calls — used by ground-track
    generation and pass prediction (240–5,760 epochs per request).

    Returns arrays: teme_pos (N,3) km, teme_vel (N,3) km/s, ecef (N,3) m,
    lat/lon/alt (N,) plus the input times.  Epochs with SGP4 errors are
    masked out of every output array.
    """
    if not times:
        raise PropagationError("Empty time list")
    jd = np.empty(len(times))
    fr = np.empty(len(times))
    for i, t in enumerate(times):
        j, f = _jday_utc(t)
        jd[i], fr[i] = j, f

    e, r, v = satrec.sgp4_array(jd, fr)
    ok = e == 0
    if not ok.any():
        raise PropagationError(f"SGP4 failed for all {len(times)} epochs (first code {e[0] if len(e) else -1})")

    r = r[ok]                     # (N,3) km TEME
    v = v[ok]                     # (N,3) km/s TEME
    t_ok = [t for t, k in zip(times, ok) if k]

    r_m = r * 1000.0
    v_mps = v * 1000.0

    # GMST per epoch → rotation matrices
    gmst = np.radians(np.array([coords.gmst_deg(t) for t in t_ok]))
    c, s = np.cos(gmst), np.sin(gmst)
    x, y, z = r_m[:, 0], r_m[:, 1], r_m[:, 2]
    ecef = np.empty_like(r_m)
    ecef[:, 0] = c * x + s * y
    ecef[:, 1] = -s * x + c * y
    ecef[:, 2] = z

    vx, vy, vz = v_mps[:, 0], v_mps[:, 1], v_mps[:, 2]
    vr_x = c * vx + s * vy
    vr_y = -s * vx + c * vy
    om = coords.EARTH_OMEGA
    vecef = np.empty_like(v_mps)
    vecef[:, 0] = vr_x + om * ecef[:, 1]
    vecef[:, 1] = vr_y - om * ecef[:, 0]
    vecef[:, 2] = vz

    lat = np.empty(len(t_ok))
    lon = np.empty(len(t_ok))
    alt = np.empty(len(t_ok))
    for i in range(len(t_ok)):
        la, lo, al = coords.geodetic_from_ecef(ecef[i])
        lat[i], lon[i], alt[i] = la, lo, al

    speed = np.sqrt((vecef ** 2).sum(axis=1))
    return {
        "times": t_ok,
        "r_teme_m": r_m,
        "v_teme_mps": v_mps,
        "r_ecef_m": ecef,
        "v_ecef_mps": vecef,
        "lat_deg": lat,
        "lon_deg": lon,
        "alt_m": alt,
        "speed_mps": speed,
    }


def az_el_series(satrec: Satrec, times: list[datetime],
                 gs_lat: float, gs_lon: float, gs_alt_m: float) -> list[dict]:
    """Topocentric Az/El/Range for a batch of epochs (ground-station geometry)."""
    gs_ecef = np.array(coords.ecef_from_geodetic(gs_lat, gs_lon, gs_alt_m))
    batch = propagate_batch(satrec, times)
    out = []
    for i in range(len(batch["times"])):
        se = batch["r_ecef_m"][i]
        sv = batch["v_ecef_mps"][i]
        d = se - gs_ecef
        rng = float(np.linalg.norm(d))
        lat = math.radians(gs_lat)
        lon = math.radians(gs_lon)
        sl, cl = math.sin(lat), math.cos(lat)
        so, co = math.sin(lon), math.cos(lon)
        e_vec = np.array((-so, co, 0.0))
        n_vec = np.array((-sl * co, -sl * so, cl))
        u_vec = np.array((cl * co, cl * so, sl))
        east, north, up = float(d @ e_vec), float(d @ n_vec), float(d @ u_vec)
        az = math.degrees(math.atan2(east, north)) % 360.0
        el = math.degrees(math.atan2(up, math.hypot(east, north)))
        los_rate = float(d @ sv) / rng if rng > 1e-6 else 0.0
        out.append({
            "utc": batch["times"][i],
            "azimuth_deg": az,
            "elevation_deg": el,
            "range_m": rng,
            "range_rate_mps": los_rate,
            "lat_deg": float(batch["lat_deg"][i]),
            "lon_deg": float(batch["lon_deg"][i]),
            "alt_m": float(batch["alt_m"][i]),
        })
    return out
