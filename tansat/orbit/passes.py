"""
tansat/orbit/passes.py
──────────────────────
Pass prediction: AOS / TCA / LOS / maximum elevation / pass azimuths from
SGP4 propagation against the ground-station geometry.

Algorithm (architecture §29) — numerical search in two stages:

1. Coarse scan.  Propagate the requested window (default 48 h) on a 30 s
   grid, compute elevation at the ground station, and find sign changes of
   (elevation − min_elevation).  A rising edge opens a candidate pass, the
   matching falling edge closes it.  Cost: ~5,760 vectorized SGP4 calls.

2. Refinement.
   • AOS/LOS: bisection on f(t) = elevation(t) − mask, converging to < 1 s.
     (Bisection is bracketed and unconditionally stable; secant would be
     faster but needs care near grazing passes.)
   • TCA/max-elevation: golden-section search on −elevation between AOS and
     LOS.  The elevation curve is unimodal within one pass (geometry of a
     straight-ish LOS arc over a spherical station), so golden section is
     guaranteed to find the global max of that pass.

Elevation values come from the WGS84 topocentric solution in coords.py —
the flat-earth approximation would put AOS/LOS minutes off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from tansat.orbit import propagator

logger = logging.getLogger(__name__)

COARSE_STEP_S      = 30.0
BISECT_TOL_S       = 0.5
GOLDEN_MAX_ITER    = 60
GOLDEN_TOL_S       = 0.25


def _elevation_at(satrec, utc: datetime, gs_lat: float, gs_lon: float,
                  gs_alt_m: float) -> float:
    """Single-epoch elevation (deg) — used by the refinement searches."""
    r = propagator.propagate(satrec, utc)
    from tansat.orbit import coords
    sat_ecef = coords.ecef_from_geodetic(r["lat_deg"], r["lon_deg"], r["alt_m"])
    geo = coords.topocentric_az_el_range(gs_lat, gs_lon, gs_alt_m, sat_ecef)
    return geo["elevation_deg"]


def _bisect_rising(satrec, t_lo: datetime, t_hi: datetime, el_lo: float,
                   el_hi: float, mask_deg: float,
                   gs_lat: float, gs_lon: float, gs_alt_m: float) -> datetime:
    """Bisect a rising edge (el_lo below mask, el_hi above) to BISECT_TOL_S."""
    a, b = t_lo, t_hi
    fa = el_lo - mask_deg
    for _ in range(40):
        dt = (b - a).total_seconds()
        if dt <= BISECT_TOL_S:
            break
        mid = a + timedelta(seconds=dt / 2.0)
        fm = _elevation_at(satrec, mid, gs_lat, gs_lon, gs_alt_m) - mask_deg
        if fa <= 0 <= fm:
            b, el_hi = mid, fm
        else:
            a, fa = mid, fm
    return a + (b - a) / 2.0


def _bisect_falling(satrec, t_lo: datetime, t_hi: datetime,
                    mask_deg: float,
                    gs_lat: float, gs_lon: float, gs_alt_m: float) -> datetime:
    """Bisect a falling edge (el_lo above mask, el_hi below) to BISECT_TOL_S."""
    a, b = t_lo, t_hi
    for _ in range(40):
        dt = (b - a).total_seconds()
        if dt <= BISECT_TOL_S:
            break
        mid = a + timedelta(seconds=dt / 2.0)
        fm = _elevation_at(satrec, mid, gs_lat, gs_lon, gs_alt_m) - mask_deg
        if fm <= 0:
            b = mid
        else:
            a = mid
    return a + (b - a) / 2.0


def _golden_max(satrec, t_lo: datetime, t_hi: datetime,
                gs_lat: float, gs_lon: float, gs_alt_m: float) -> tuple[datetime, float]:
    """Golden-section search for the elevation maximum between t_lo and t_hi.

    Returns (t_max, max_elevation_deg).
    """
    inv_phi = (5 ** 0.5 - 1) / 2.0          # 0.618…
    a, b = t_lo, t_hi
    span = (b - a).total_seconds()
    c = b - timedelta(seconds=inv_phi * span)
    d = a + timedelta(seconds=inv_phi * span)
    fc = _elevation_at(satrec, c, gs_lat, gs_lon, gs_alt_m)
    fd = _elevation_at(satrec, d, gs_lat, gs_lon, gs_alt_m)
    for _ in range(GOLDEN_MAX_ITER):
        if (b - a).total_seconds() <= GOLDEN_TOL_S:
            break
        if fc >= fd:
            b, d, fd = d, c, fc
            span = (b - a).total_seconds()
            c = b - timedelta(seconds=inv_phi * span)
            fc = _elevation_at(satrec, c, gs_lat, gs_lon, gs_alt_m)
        else:
            a, c, fc = c, d, fd
            span = (b - a).total_seconds()
            d = a + timedelta(seconds=inv_phi * span)
            fd = _elevation_at(satrec, d, gs_lat, gs_lon, gs_alt_m)
    t_max = a + (b - a) / 2.0
    return t_max, _elevation_at(satrec, t_max, gs_lat, gs_lon, gs_alt_m)


def _geom_at(satrec, utc, gs_lat, gs_lon, gs_alt_m) -> dict:
    """Full geometry (az/el/range) at one epoch for pass endpoints."""
    r = propagator.propagate(satrec, utc)
    from tansat.orbit import coords
    sat_ecef = coords.ecef_from_geodetic(r["lat_deg"], r["lon_deg"], r["alt_m"])
    geo = coords.topocentric_az_el_range(gs_lat, gs_lon, gs_alt_m, sat_ecef)
    return {"utc": utc, **geo}


def find_passes(satrec, gs_lat: float, gs_lon: float, gs_alt_m: float,
                start: datetime | None = None, hours: float = 48.0,
                min_elevation_deg: float = 0.0,
                coarse_step_s: float = COARSE_STEP_S) -> list[dict]:
    """Predict passes over [start, start + hours].

    Returns a list of passes sorted by AOS:
        {aos_utc, tca_utc, los_utc, max_elevation_deg, aos_azimuth_deg,
         los_azimuth_deg, duration_s, tca_range_km}
    Passes whose entire arc stays at/below the mask are excluded (they never
    produce a rising edge, so this is automatic), and grazing passes whose
    refined max elevation fails to clear the mask are dropped as artifacts.
    """
    from tansat.orbit import coords

    start = start or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=hours)

    n = int(hours * 3600 / coarse_step_s) + 1
    times = [start + timedelta(seconds=coarse_step_s * i) for i in range(n)]
    series = propagator.az_el_series(satrec, times, gs_lat, gs_lon, gs_alt_m)
    els = [p["elevation_deg"] for p in series]

    passes: list[dict] = []
    in_pass = False
    pass_start_idx = 0

    for i, el in enumerate(els):
        above = el > min_elevation_deg
        if above and not in_pass:
            in_pass = True
            pass_start_idx = max(i - 1, 0)
        elif not above and in_pass:
            in_pass = False
            pass_end_idx = min(i, len(times) - 1)

            t_aos = _bisect_rising(satrec, times[pass_start_idx], times[pass_start_idx + 1],
                                   els[pass_start_idx], els[pass_start_idx + 1],
                                   min_elevation_deg, gs_lat, gs_lon, gs_alt_m)
            t_los = _bisect_falling(satrec, times[pass_end_idx - 1], times[pass_end_idx],
                                    min_elevation_deg, gs_lat, gs_lon, gs_alt_m)
            if t_los <= t_aos:
                continue

            t_tca, max_el = _golden_max(satrec, t_aos, t_los, gs_lat, gs_lon, gs_alt_m)
            if max_el <= min_elevation_deg:
                continue    # grazing artifact of the coarse grid

            aos_geom = _geom_at(satrec, t_aos, gs_lat, gs_lon, gs_alt_m)
            los_geom = _geom_at(satrec, t_los, gs_lat, gs_lon, gs_alt_m)
            tca_r = propagator.propagate(satrec, t_tca)
            tca_ecef = coords.ecef_from_geodetic(tca_r["lat_deg"], tca_r["lon_deg"],
                                                 tca_r["alt_m"])
            tca_geo = coords.topocentric_az_el_range(gs_lat, gs_lon, gs_alt_m, tca_ecef)

            passes.append({
                "aos_utc": t_aos,
                "tca_utc": t_tca,
                "los_utc": t_los,
                "max_elevation_deg": round(max_el, 2),
                "aos_azimuth_deg": round(aos_geom["azimuth_deg"], 1),
                "los_azimuth_deg": round(los_geom["azimuth_deg"], 1),
                "duration_s": round((t_los - t_aos).total_seconds(), 1),
                "tca_range_km": round(tca_geo["range_m"] / 1000.0, 1),
            })

    # A pass still open at window end: refine what we have, mark LOS unknown.
    if in_pass:
        pass_end_idx = len(times) - 1
        t_aos = _bisect_rising(satrec, times[pass_start_idx], times[pass_start_idx + 1],
                               els[pass_start_idx], els[pass_start_idx + 1],
                               min_elevation_deg, gs_lat, gs_lon, gs_alt_m)
        t_tca, max_el = _golden_max(satrec, t_aos, times[-1], gs_lat, gs_lon, gs_alt_m)
        if max_el > min_elevation_deg:
            aos_geom = _geom_at(satrec, t_aos, gs_lat, gs_lon, gs_alt_m)
            passes.append({
                "aos_utc": t_aos,
                "tca_utc": t_tca,
                "los_utc": None,
                "max_elevation_deg": round(max_el, 2),
                "aos_azimuth_deg": round(aos_geom["azimuth_deg"], 1),
                "los_azimuth_deg": None,
                "duration_s": None,
                "tca_range_km": None,
            })

    return passes


def next_pass(passes: list[dict], now: datetime | None = None) -> dict | None:
    """First pass in the list whose AOS is still in the future (or ongoing)."""
    now = now or datetime.now(timezone.utc)
    for p in passes:
        if p["los_utc"] is None or p["los_utc"] > now:
            return p
    return None
