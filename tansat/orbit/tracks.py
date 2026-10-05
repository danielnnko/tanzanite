"""
tansat/orbit/tracks.py
──────────────────────
Ground-track generation: predicted (TLE/SGP4) and observed (GPS/ORB) tracks,
plus the International-Date-Line / antimeridian splitting logic.

Predicted track algorithm (architecture §17):

    now (UTC) → timestamp grid (30 s) → SGP4 batch → TEME → ECEF → geodetic
    → (lat, lon, alt, elevation-at-GS per point) → polyline segments

Default window: −30 min … +95 min (a LEO CubeSat at ~95 min period gets one
full revolution of future track plus a continuity stub of the past).  One
revolution is the operational sweet spot: multiple revolutions just stack
nearly-parallel lines 22–25° apart and hide the current pass.

Antimeridian handling (§21): consecutive points with |Δlon| > 180° indicate a
crossing; the polyline is SPLIT there so Leaflet never draws a segment
across the whole world.  Points keep their unwrapped longitudes (±180±),
which render correctly on both sides of the seam with worldCopyJump.

Pole handling (§22): |lat| > 89.9° points are dropped (numerical singularity
of the geodetic mapping; a real LEO track touches ~inclination max, so this
only fires on degenerate input).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from tansat.orbit import coords, propagator

logger = logging.getLogger(__name__)

# ── Track defaults ──────────────────────────────────────────────────
TRACK_PAST_MIN   = 30.0     # continuity stub behind "now"
TRACK_FUTURE_MIN = 95.0     # ≈ one LEO revolution
TRACK_STEP_S     = 30.0     # 7.5 km/s ⇒ ~225 km spacing
POLE_LAT_LIMIT   = 89.9     # degrees; beyond this the point is dropped


def split_on_dateline(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Split a lat/lon polyline at antimeridian crossings.

    A segment between consecutive points is crossed when |lon2 − lon1| > 180°
    (e.g. +179 → −179 is a 2° real step, a 358° naive step).  The polyline is
    cut at each crossing, producing independently drawable segments.  Works
    identically for live-appended and back-filled point lists.
    """
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    prev_lon = None
    for lat, lon in points:
        if prev_lon is not None and abs(lon - prev_lon) > 180.0:
            if len(current) >= 2:
                segments.append(current)
            current = []
        current.append((lat, lon))
        prev_lon = lon
    if len(current) >= 2:
        segments.append(current)
    return segments


def generate_predicted_track(satrec, now: datetime | None = None,
                             gs_lat: float | None = None,
                             gs_lon: float | None = None,
                             gs_alt_m: float = 0.0,
                             past_min: float = TRACK_PAST_MIN,
                             future_min: float = TRACK_FUTURE_MIN,
                             step_s: float = TRACK_STEP_S) -> dict:
    """Generate the predicted ground track around `now`.

    Returns {
      points:  [{utc, lat, lon, alt_m, el_deg, azimuth_deg, in_future}, …]
      segments: [[(lat, lon), …], …]          dateline-split polylines
      future_segments, past_segments:         same, split at `now`
      generated_at: ISO string
    }
    `now` defaults to real UTC; gs_* are optional (elevation computed only
    when the station position is supplied).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    t0 = now - timedelta(minutes=past_min)
    n_past = int(past_min * 60 / step_s)
    n_fut = int(future_min * 60 / step_s)
    times = [t0 + timedelta(seconds=step_s * i) for i in range(n_past + n_fut + 1)]

    batch = propagator.propagate_batch(satrec, times)

    az_series = None
    if gs_lat is not None and gs_lon is not None:
        az_series = propagator.az_el_series(satrec, times, gs_lat, gs_lon, gs_alt_m)

    points = []
    for i, t in enumerate(batch["times"]):
        lat = float(batch["lat_deg"][i])
        if abs(lat) > POLE_LAT_LIMIT:
            continue
        pt = {
            "utc": t,
            "lat": lat,
            "lon": float(batch["lon_deg"][i]),
            "alt_m": float(batch["alt_m"][i]),
            "in_future": t > now,
        }
        if az_series is not None:
            pt["azimuth_deg"] = az_series[i]["azimuth_deg"]
            pt["elevation_deg"] = az_series[i]["elevation_deg"]
        points.append(pt)

    latlons = [(p["lat"], p["lon"]) for p in points]
    segments = split_on_dateline(latlons)
    future_ll = [(p["lat"], p["lon"]) for p in points if p["in_future"]]
    past_ll = [(p["lat"], p["lon"]) for p in points if not p["in_future"]]

    return {
        "points": points,
        "segments": segments,
        "future_segments": split_on_dateline(future_ll),
        "past_segments": split_on_dateline(past_ll),
        "generated_at": now.isoformat(),
    }


def observed_track_segments(fixes: list[dict], lat_key: str = "lat",
                            lon_key: str = "lon") -> list[list[tuple[float, float]]]:
    """Dateline-split polyline segments from observed fixes (GPS or ORB).

    `fixes` must be in chronological order; each item supplies lat/lon via
    the given keys.  Non-finite and pole-singular values are dropped.
    """
    pts = []
    for f in fixes:
        try:
            lat, lon = float(f[lat_key]), float(f[lon_key])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(lat) > POLE_LAT_LIMIT:
            continue
        pts.append((lat, lon))
    return split_on_dateline(pts)


def coverage_footprint(subsat_lat: float, subsat_lon: float, alt_m: float,
                       min_elevation_deg: float = 0.0) -> dict:
    """Visibility footprint circle (geometric horizon or elevation-mask cut).

    Central angle ψ between sub-satellite point and the edge of the
    visibility circle, from planar-trig geometry on a spherical Earth:

        ψ = arccos( R/(R+h) · cos(el) ) − el

    el = 0° gives the geometric horizon (largest circle); el = 5° gives the
    circle inside which the satellite is seen ≥ 5° above the horizon — the
    operationally relevant "communication coverage" for a ground station with
    an elevation mask.  These are NOT the same thing and are labelled
    separately in the UI (architecture §27).

    Returns {center, radius_km, central_angle_deg, horizon_km} — the Leaflet
    side draws it with L.Circle (geodesic; correct near poles).
    """
    r = 6_371_000.0
    h = max(alt_m, 1.0)
    el = math.radians(min_elevation_deg)
    ratio = (r / (r + h)) * math.cos(el)
    psi = math.acos(min(1.0, max(-1.0, ratio))) - el
    radius_km = r * psi / 1000.0
    # Slant range to the footprint edge (law of cosines in the triangle)
    horizon_km = math.sqrt(r * r + (r + h) ** 2 - 2 * r * (r + h) * math.cos(psi)) / 1000.0
    return {
        "center": [subsat_lat, subsat_lon],
        "radius_km": radius_km,
        "central_angle_deg": math.degrees(psi),
        "horizon_km": horizon_km,
        "min_elevation_deg": min_elevation_deg,
    }
