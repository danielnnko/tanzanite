"""
tansat/orbit/coords.py
──────────────────────
Reference-frame transformations for the tracking subsystem.

Pipeline implemented here (matches the architecture decision):

    TLE → SGP4 → TEME (inertial)  ──(GMST rotation)──►  ECEF  ──(WGS84)──►  geodetic
    geodetic GS position ──(WGS84)──► ECEF ──(topocentric)──► Az/El/Range/range-rate

Physics notes (kept explicit for the academic report):

• SGP4 outputs TEME (True Equator / Mean Equinox).  The dominant TEME→ECEF
  effect is Earth rotation, captured by GMST.  We deliberately OMIT polar
  motion (~0.3 arcsec), UT1−UTC (<0.9 s) and precession/nutation corrections:
  each contributes << 100 m of ground error, which is far below the 1–3 km
  accuracy of a TLE itself.  Documented simplification, not an oversight.
• ECEF→geodetic uses the closed-form Bowring/Borkowski-style iteration
  (Olgiati 3-iteration variant) on the WGS84 ellipsoid.  Accuracy ~1e-9
  relative — far beyond what a ground track needs.
• Topocentric Az/El uses the standard ENU (East-North-Up) projection of the
  satellite − observer ECEF vector, then converts ENU→azimuth/elevation.
  This replaces tansat.physics.calculate_azimuth_and_range, whose planar
  atan2(dalt, dist) elevation is wrong by up to ~10° at LEO ranges
  (e.g. true horizon at 520 km: el=0°, slant≈2,570 km vs old formula
  el≈3.2°, slant≈9,270 km).

All time arguments are timezone-aware UTC datetimes (see §16 of the
architecture: UTC is the single internal time standard).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# ── WGS84 constants ─────────────────────────────────────────────────
WGS84_A      = 6_378_137.0          # semi-major axis (m)
WGS84_F      = 1.0 / 298.257_223_563
WGS84_E2     = WGS84_F * (2.0 - WGS84_F)   # first eccentricity squared
WGS84_B      = WGS84_A * (1.0 - WGS84_F)   # semi-minor axis (m)

# Earth rotation rate (IAU 1980 value, rad/s) — used for range-rate
EARTH_OMEGA  = 7.292_115_146_706_979e-5

# IAU 1982 GMST polynomial (Vallado, Fundamentals of Astrodynamics, Eq. 3-42)
_GMST0_REF_J2000 = 2_451_545.0      # J2000.0 Julian date


def julian_date(utc: datetime) -> float:
    """Julian Date from a timezone-aware UTC datetime (CELEMECH-style).

    Uses the exact civil-calendar formula; valid over the full Python
    datetime range.  UTC≈TT difference (~69 s) is irrelevant at TLE accuracy
    but is documented: GMST from UT1 (≈UTC±0.9 s) → <0.4 arcsec of frame error.
    """
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    y, m = utc.year, utc.month
    d = utc.day + (utc.hour + (utc.minute + (utc.second + utc.microsecond * 1e-6) / 60.0) / 60.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    jd = math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5
    return jd


def gmst_deg(utc: datetime) -> float:
    """Greenwich Mean Sidereal Time in degrees [0, 360).

    IAU-1982 polynomial (Vallado Eq. 3-42):
        θ_GMST = 280.46061837 + 360.98564736629·(JD − JD_J2000)
                 + 0.000387933·T² + T³/38710000
    with T in Julian centuries from J2000.  The T²/T³ terms are < 1 arcmin
    over this project's lifetime but are kept for completeness.
    """
    jd = julian_date(utc)
    t = (jd - _GMST0_REF_J2000) / 36_525.0
    gmst = (280.46061837
            + 360.98564736629 * (jd - _GMST0_REF_J2000)
            + 0.000387933 * t * t
            + t * t * t / 38_710_000.0)
    return gmst % 360.0


def teme_to_ecef(r_teme_m, utc: datetime):
    """Rotate a TEME position vector into ECEF (Earth-fixed).

    At our accuracy the TEME→PEF rotation is exactly the GMST rotation about
    the Z axis (nutation/precession corrections are below TLE noise):
        r_ecef = R3(gmst) · r_teme

    r_teme_m: (x, y, z) in metres, TEME frame.
    Returns   (x, y, z) in metres, ECEF frame.
    """
    th = math.radians(gmst_deg(utc))
    c, s = math.cos(th), math.sin(th)
    x, y, z = r_teme_m
    return (c * x + s * y, -s * x + c * y, z)


def ecef_to_teme(r_ecef_m, utc: datetime):
    """Inverse of teme_to_ecef (R3(−GMST))."""
    th = math.radians(gmst_deg(utc))
    c, s = math.cos(th), math.sin(th)
    x, y, z = r_ecef_m
    return (c * x - s * y, s * x + c * y, z)


def geodetic_from_ecef(r_ecef_m, max_iter: int = 8, tol: float = 1e-9):
    """ECEF (m) → WGS84 geodetic (lat_deg, lon_deg, alt_m).

    Closed-form latitude seed + altitude iteration (Olgiati / Borkowski
    style).  Converges in 3–4 iterations to sub-millimetre altitude.
    """
    x, y, z = r_ecef_m
    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    # Degenerate cases
    if p < 1e-6:                      # on the spin axis
        lat = math.copysign(math.pi / 2.0, z) if abs(z) > 1e-6 else 0.0
        alt = abs(z) - WGS84_B
        return math.degrees(lat), math.degrees(lon), alt
    if abs(z) < 1e-6:                 # on the equatorial plane
        lat = 0.0
        alt = p - WGS84_A
        return math.degrees(lat), math.degrees(lon), alt

    e2 = WGS84_E2
    ep2 = e2 / (1.0 - e2)
    # Bowring closed-form seed for latitude
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    lat = math.atan2(z + ep2 * WGS84_B * math.sin(theta) ** 3,
                     p - e2 * WGS84_A * math.cos(theta) ** 3)
    # Newton refinement of latitude + altitude
    n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    for _ in range(max_iter):
        n_new = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
        alt_new = p / math.cos(lat) - n_new
        lat_new = math.atan2(z, p * (1.0 - e2 * n_new / (n_new + alt_new)))
        if abs(lat_new - lat) < tol and abs(alt_new - alt) < 1e-6:
            lat, alt = lat_new, alt_new
            break
        lat, alt = lat_new, alt_new
    return math.degrees(lat), math.degrees(lon), alt


def ecef_from_geodetic(lat_deg: float, lon_deg: float, alt_m: float):
    """WGS84 geodetic → ECEF (m).  Standard closed form."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    e2 = WGS84_E2
    n = WGS84_A / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    return ((n + alt_m) * math.cos(lat) * math.cos(lon),
            (n + alt_m) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - e2) + alt_m) * math.sin(lat))


def topocentric_az_el_range(gs_lat_deg: float, gs_lon_deg: float, gs_alt_m: float,
                            sat_ecef_m, sat_vel_ecef_mps=None):
    """Ground-station pointing geometry, WGS84-correct.

    Replaces tansat.physics.calculate_azimuth_and_range for orbital targets:
    the old planar formula overestimates elevation by up to ~10° mid-pass and
    slant range by ~3.5× near the horizon (see module docstring).

    Returns dict:
        azimuth_deg    0=N, 90=E, clockwise
        elevation_deg  -90..+90 (negative = below the local horizon plane)
        range_m        slant range (m)
        range_rate_mps metres/second along the LOS (positive = receding),
                       None if no velocity given
    """
    gs_ecef = ecef_from_geodetic(gs_lat_deg, gs_lon_deg, gs_alt_m)
    dx = sat_ecef_m[0] - gs_ecef[0]
    dy = sat_ecef_m[1] - gs_ecef[1]
    dz = sat_ecef_m[2] - gs_ecef[2]
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)

    # ENU unit vectors at the observer (geodetic, WGS84)
    lat = math.radians(gs_lat_deg)
    lon = math.radians(gs_lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    # East / North / Up basis
    e = (   -so,      co, 0.0)
    n = (-sl * co, -sl * so, cl)
    u = ( cl * co,  cl * so, sl)

    east  = dx * e[0] + dy * e[1] + dz * e[2]
    north = dx * n[0] + dy * n[1] + dz * n[2]
    up    = dx * u[0] + dy * u[1] + dz * u[2]

    az = math.degrees(math.atan2(east, north)) % 360.0
    el = math.degrees(math.atan2(up, math.hypot(east, north)))

    rate = None
    if sat_vel_ecef_mps is not None and rng > 1e-6:
        vx, vy, vz = sat_vel_ecef_mps
        los_vel = (dx * (vx - (-EARTH_OMEGA * dy)) +
                   dy * (vy - (  EARTH_OMEGA * dx)) +
                   dz * vz) / rng
        rate = los_vel

    return {
        "azimuth_deg": az,
        "elevation_deg": el,
        "range_m": rng,
        "range_rate_mps": rate,
    }


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a sphere of Earth's mean radius (km)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def subpoint_from_teme(r_teme_m, utc: datetime):
    """TEME position → (lat_deg, lon_deg, alt_m) geodetic subpoint."""
    return geodetic_from_ecef(teme_to_ecef(r_teme_m, utc))
