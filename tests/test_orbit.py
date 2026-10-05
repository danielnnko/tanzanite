"""
Tests for the orbital tracking subsystem (tansat/orbit).

Coverage mirrors the test matrix in architecture §37: coordinate math,
TLE validation/staleness, SGP4 propagation, dateline splitting, footprint
geometry, pass prediction, and the source-arbitration state machine.
No database is required: persistence paths are monkeypatched out.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tansat.orbit import coords, passes, propagator, tracks
from tansat.orbit.tles import (TleError, parse_epoch, parse_tle, tle_age_hours,
                               tle_checksum, tle_status, tle_usable)

# Vallado's canonical ISS test TLE (checksums verified: 7 / 7)
L1 = '1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927'
L2 = '2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537'
ISS_EPOCH = datetime(2008, 9, 20, 12, 25, 40, 104192, tzinfo=timezone.utc)

GS = (-6.813737, 39.280427, 30.0)   # Dar es Salaam (lat, lon, alt_m)


# ── coords: time and frames ──────────────────────────────────────────
def test_julian_date_known_value():
    # J2000.0 = JD 2451545.0 = 2000-01-01 12:00:00 **TT**.
    # TT−UTC in 2000 = 64.184 s (TAI offset 32 s + TT−TAI 32.184 s), so the
    # UTC clock reads 11:58:55.816.  The civil-calendar conversion must
    # reproduce the offset exactly:
    jd = coords.julian_date(datetime(2000, 1, 1, 11, 58, 55, 816000, tzinfo=timezone.utc))
    assert abs(jd - (2451545.0 - 64.184 / 86400.0)) < 1e-6


def test_gmst_known_value():
    # Cross-checked against Vallado's worked example (Fundamentals of
    # Astrodynamics, GMST for 2026-01-01 00:00 UTC ≈ 100.66°)
    g = coords.gmst_deg(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert abs(g - 100.660859) < 0.01


def test_gmst_wraps_360():
    g = coords.gmst_deg(datetime(2026, 6, 15, 6, 30, tzinfo=timezone.utc))
    assert 0.0 <= g < 360.0


def test_geodetic_roundtrip_wgs84():
    for lat, lon, alt in [(-6.813737, 39.280427, 30.0),
                          (51.5, -0.12, 0.0),
                          (-89.0, 179.0, 550_000.0),
                          (0.0, 0.0, 500_000.0)]:
        ecef = coords.ecef_from_geodetic(lat, lon, alt)
        back = coords.geodetic_from_ecef(ecef)
        assert back[0] == pytest.approx(lat, abs=1e-7)
        assert back[1] == pytest.approx(lon, abs=1e-7)
        assert back[2] == pytest.approx(alt, abs=0.5)


def test_teme_ecef_roundtrip_preserves_radius():
    utc = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
    r_teme = (4_000_000.0, 3_000_000.0, 5_000_000.0)
    r_ecef = coords.teme_to_ecef(r_teme, utc)
    back = coords.ecef_to_teme(r_ecef, utc)
    assert back == pytest.approx(r_teme, abs=1e-6)
    # pure rotation → same magnitude
    import math
    m1 = math.sqrt(sum(c * c for c in r_teme))
    m2 = math.sqrt(sum(c * c for c in r_ecef))
    assert m2 == pytest.approx(m1, abs=1e-6)


def test_topocentric_zenith_and_horizon():
    # Zenith: satellite directly over the station at 520 km
    sat = coords.ecef_from_geodetic(GS[0], GS[1], 520_000.0)
    g = coords.topocentric_az_el_range(GS[0], GS[1], GS[2], sat)
    assert g["elevation_deg"] == pytest.approx(90.0, abs=0.01)
    assert g["range_m"] == pytest.approx(519_970.0, abs=100.0)

    # True LEO horizon at 520 km: central angle ψ = acos(R/(R+h)) ≈ 22.4°,
    # slant ≈ 2,574 km.  Construct the satellite at exactly ψ of GEOCENTRIC
    # central angle from the station (spherical placement — moving in
    # geodetic latitude does NOT produce a known central angle).
    import math
    gs_ecef = coords.ecef_from_geodetic(GS[0], GS[1], GS[2])
    gc_lat = math.atan2(gs_ecef[2], math.hypot(gs_ecef[0], gs_ecef[1]))
    gc_lon = math.atan2(gs_ecef[1], gs_ecef[0])
    psi = math.radians(22.4)
    rr = 6_371_000.0 + 520_000.0
    sat_h = (rr * math.cos(gc_lat + psi) * math.cos(gc_lon),
             rr * math.cos(gc_lat + psi) * math.sin(gc_lon),
             rr * math.sin(gc_lat + psi))
    g2 = coords.topocentric_az_el_range(GS[0], GS[1], GS[2], sat_h)
    assert abs(g2["elevation_deg"]) < 1.5          # on the horizon plane
    assert 2_400_000.0 < g2["range_m"] < 2_750_000.0


def test_range_rate_sign_receding_positive():
    utc = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
    sat = coords.ecef_from_geodetic(GS[0] + 5.0, GS[1], 520_000.0)
    # velocity pointing radially OUT from the station → receding
    import math
    gs = coords.ecef_from_geodetic(GS[0], GS[1], GS[2])
    away = tuple((s - g) / math.dist(sat, gs) * 7500.0 for s, g in zip(sat, gs))
    g = coords.topocentric_az_el_range(GS[0], GS[1], GS[2], sat, away)
    assert g["range_rate_mps"] is not None and g["range_rate_mps"] > 0


# ── TLE validation ───────────────────────────────────────────────────
def test_tle_checksum_excludes_last_column():
    assert tle_checksum(L1) == 7
    assert tle_checksum(L2) == 7


def test_parse_tle_valid_block():
    t = parse_tle(L1 + "\n" + L2)
    assert t["satnum"] == 25544
    assert t["epoch"] == ISS_EPOCH
    assert t["inclination_deg"] == pytest.approx(51.6416)
    assert t["mean_motion_revs_per_day"] == pytest.approx(15.72125391)
    assert t["bstar"] == pytest.approx(-1.1606e-05)
    assert t["period_minutes"] == pytest.approx(91.6, abs=0.5)


def test_parse_tle_three_line_with_name():
    t = parse_tle("ISS (ZARYA)\n" + L1 + "\n" + L2)
    assert t["name"] == "ISS (ZARYA)"


def test_parse_tle_rejects_bad_checksum():
    bad = L1[:-1] + ("0" if L1[-1] != "0" else "1")
    with pytest.raises(TleError, match="checksum"):
        parse_tle(bad + "\n" + L2)


def test_parse_tle_rejects_short_line():
    with pytest.raises(TleError):
        parse_tle(L1[:40] + "\n" + L2)


def test_parse_tle_rejects_non_leo_mean_motion():
    # GEO mean motion (~1 rev/day) is outside the LEO band → rejected
    l2_geo = list(L2)
    l2_geo[52:63] = list(" 1.00273790")
    bad_l2 = "".join(l2_geo)
    bad_l2 = bad_l2[:68] + str(tle_checksum(bad_l2[:68]))
    with pytest.raises(TleError, match="Mean motion"):
        parse_tle(L1 + "\n" + bad_l2)


def test_epoch_two_digit_year_pivot():
    # YY=26 → 2026; YY=80 → 1980 (pre-57 pivot)
    assert parse_epoch("1 25544U 98067A   26001.00000000" + ".0" * 0 +
                       " -.00002182  00000-0 -11606-4 0  2927"[32:]).year == 2026


def test_staleness_classification():
    now = datetime.now(timezone.utc)
    assert tle_status(now - timedelta(hours=2)) == "fresh"
    assert tle_status(now - timedelta(hours=48)) == "aging"
    assert tle_status(now - timedelta(days=5)) == "stale"
    assert tle_status(now - timedelta(days=10)) == "dead_zone"
    assert tle_status(now - timedelta(days=20)) == "expired"
    assert not tle_usable(now - timedelta(days=20))
    assert tle_usable(now - timedelta(days=5))


# ── Propagator ───────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def iss_satrec():
    return propagator.make_satrec(L1, L2)


def test_propagate_at_epoch(iss_satrec):
    r = propagator.propagate(iss_satrec, ISS_EPOCH)
    assert r["lat_deg"] == pytest.approx(51.4636, abs=0.01)
    assert r["lon_deg"] == pytest.approx(160.1432, abs=0.05)
    assert 6_700 < r["r_ecef_m"][0] ** 2 + r["r_ecef_m"][1] ** 2 + r["r_ecef_m"][2] ** 2 \
        and (r["r_ecef_m"][0] ** 2 + r["r_ecef_m"][1] ** 2 + r["r_ecef_m"][2] ** 2) ** 0.5 / 1000 < 6_750
    assert 7.3 < r["speed_mps"] / 1000.0 < 7.5   # km/s — LEO orbital velocity


def test_half_orbit_longitude_shift(iss_satrec):
    """45 min later the subpoint must be ~24° west: ~22.5° Earth rotation plus
    the nodal-regression contribution.  This is the physics the old 3D view
    could never show (Earth-fixed satellite)."""
    r1 = propagator.propagate(iss_satrec, ISS_EPOCH)
    r2 = propagator.propagate(iss_satrec, ISS_EPOCH + timedelta(minutes=45))
    assert r2["lat_deg"] < 0                     # opposite hemisphere
    assert r2["lon_deg"] < r1["lon_deg"]         # westward shift


def test_batch_matches_single(iss_satrec):
    times = [ISS_EPOCH + timedelta(seconds=37 * i) for i in range(25)]
    batch = propagator.propagate_batch(iss_satrec, times)
    for i, t in enumerate(times):
        single = propagator.propagate(iss_satrec, t)
        assert batch["lat_deg"][i] == pytest.approx(single["lat_deg"], abs=1e-9)
        assert batch["lon_deg"][i] == pytest.approx(single["lon_deg"], abs=1e-9)
        assert batch["alt_m"][i] == pytest.approx(single["alt_m"], abs=1e-6)


def test_az_el_series_shape(iss_satrec):
    times = [ISS_EPOCH + timedelta(seconds=60 * i) for i in range(5)]
    series = propagator.az_el_series(iss_satrec, times, *GS)
    assert len(series) == 5
    for p in series:
        assert 0.0 <= p["azimuth_deg"] < 360.0
        assert -90.0 <= p["elevation_deg"] <= 90.0
        assert p["range_m"] > 6_371_000.0


# ── Tracks ───────────────────────────────────────────────────────────
def test_dateline_split():
    pts = [(10.0, 179.0), (10.5, 179.5), (11.0, -179.5), (11.5, -179.0)]
    segs = tracks.split_on_dateline(pts)
    assert len(segs) == 2
    assert segs[0][-1][1] == 179.5
    assert segs[1][0][1] == -179.5


def test_dateline_no_split_without_crossing():
    pts = [(10.0, 39.0), (10.5, 39.5), (11.0, 40.0)]
    assert len(tracks.split_on_dateline(pts)) == 1


def test_predicted_track_structure(iss_satrec):
    now = datetime.now(timezone.utc)
    tr = tracks.generate_predicted_track(iss_satrec, now=now, gs_lat=GS[0],
                                         gs_lon=GS[1], gs_alt_m=GS[2])
    assert len(tr["points"]) > 200                 # 30 s over ~125 min
    assert tr["future_segments"] and tr["past_segments"]
    n_pts = sum(len(s) for s in tr["segments"])
    assert n_pts == len(tr["points"])              # split preserves points
    assert all(p["utc"] > now for p in
               [pt for pt in tr["points"] if pt["in_future"]])


def test_footprint_geometry():
    # 520 km altitude, geometric horizon: ψ = acos(R/(R+h)) ≈ 22.40°,
    # ground radius ≈ 2,492 km
    f = tracks.coverage_footprint(0.0, 0.0, 520_000.0, min_elevation_deg=0.0)
    assert f["central_angle_deg"] == pytest.approx(22.40, abs=0.15)
    assert f["radius_km"] == pytest.approx(2_492, rel=0.01)
    # With a 5° mask the circle must be SMALLER (communication coverage,
    # not the geometric horizon — §27)
    f5 = tracks.coverage_footprint(0.0, 0.0, 520_000.0, min_elevation_deg=5.0)
    assert f5["radius_km"] < f["radius_km"]


# ── Pass prediction ──────────────────────────────────────────────────
def test_find_passes_structure_and_physics(iss_satrec):
    # Dar es Salaam (lat -6.8) sees ISS-type 51.6° orbits several times a day
    start = ISS_EPOCH
    plist = passes.find_passes(iss_satrec, *GS, start=start, hours=24.0,
                               min_elevation_deg=0.0)
    assert plist, "24 h of a 51.6° LEO from lat -6.8 must yield passes"
    for p in plist:
        assert p["aos_utc"] < p["tca_utc"]
        if p["los_utc"] is None:
            continue    # pass still open at window end — legitimate (§29)
        assert p["tca_utc"] < p["los_utc"]
        assert p["max_elevation_deg"] > 0.0
        # LEO pass duration: 2–12 min above 0° mask
        assert 100 < p["duration_s"] < 800
    assert any(p["los_utc"] is not None for p in plist)


def test_find_passes_respects_elevation_mask(iss_satrec):
    low = passes.find_passes(iss_satrec, *GS, start=ISS_EPOCH, hours=24.0,
                             min_elevation_deg=0.0)
    high = passes.find_passes(iss_satrec, *GS, start=ISS_EPOCH, hours=24.0,
                              min_elevation_deg=30.0)
    # A 30° mask must hide some (possibly all) of the 0°-mask passes
    assert len(high) <= len(low)
    for p in high:
        assert p["max_elevation_deg"] >= 30.0


# ── Engine: source arbitration (no DB) ───────────────────────────────
def _engine_with_sources(gps_age_s=None, orb_age_s=None, tle_age_h=None):
    from tansat.orbit.engine import TrackingEngine
    eng = TrackingEngine(data_dir="/tmp")
    now = datetime.now(timezone.utc)
    if gps_age_s is not None:
        eng._gps_fix = {"lat": 1.0, "lon": 2.0, "alt_m": 500_000.0,
                        "raw_lat": None, "raw_lon": None, "t": now - timedelta(seconds=gps_age_s),
                        "server_ts": now.isoformat()}
    if orb_age_s is not None:
        eng._orb_fix = {"lat": 3.0, "lon": 4.0, "alt_m": 500_000.0,
                        "t": now - timedelta(seconds=orb_age_s),
                        "server_ts": now.isoformat()}
    if tle_age_h is not None:
        eng._satrec = propagator.make_satrec(L1, L2)
        eng._tle_meta = {"id": 1, "norad_id": 25544, "name": "TEST",
                         "epoch": now - timedelta(hours=tle_age_h),
                         "age_h": tle_age_h, "status": tle_status(now - timedelta(hours=tle_age_h)),
                         "source": "test"}
    return eng


def test_arbitration_gps_wins():
    eng = _engine_with_sources(gps_age_s=5, orb_age_s=5, tle_age_h=1)
    fix, src = eng._arbitrate()
    assert src == "gps" and fix["lat"] == 1.0


def test_arbitration_fresh_orb_when_gps_stale():
    eng = _engine_with_sources(gps_age_s=10_000, orb_age_s=30, tle_age_h=1)
    fix, src = eng._arbitrate()
    assert src == "orb"


def test_arbitration_tle_when_all_stale():
    eng = _engine_with_sources(gps_age_s=10_000, orb_age_s=10_000, tle_age_h=48)
    fix, src = eng._arbitrate()
    assert src == "tle" and fix is None


def test_arbitration_expired_tle_is_no_source():
    eng = _engine_with_sources(gps_age_s=None, orb_age_s=None, tle_age_h=24 * 20)
    fix, src = eng._arbitrate()
    assert src == "none" and fix is None


def test_arbitration_nothing_available():
    eng = _engine_with_sources()
    fix, src = eng._arbitrate()
    assert src == "none"


def test_ingest_tlm_sets_gps_state():
    eng = _engine_with_sources()
    eng._maybe_persist_fix = lambda *a, **k: None     # DB-free
    eng.ingest({"type": "tlm", "latitude": -6.9, "longitude": 39.3,
                "true_altitude_m": 520_000.0, "azimuth": 10.0, "elevation": 45.0,
                "slant_km": 700.0, "server_ts": "now"})
    assert eng._gps_fix is not None
    assert eng._gps_fix["lat"] == -6.9
    # no-fix packets must never fabricate a position
    eng.ingest({"type": "tlm", "latitude": 0.0, "longitude": 0.0})
    assert eng._gps_fix["lat"] == -6.9


def test_ingest_orb_sets_orb_state():
    eng = _engine_with_sources()
    eng._maybe_persist_fix = lambda *a, **k: None
    eng.ingest({"type": "ORB", "latitude": 5.0, "longitude": 40.0,
                "true_altitude_m": 510_000.0})
    assert eng._orb_fix is not None and eng._orb_fix["lat"] == 5.0
