"""
tansat/orbit/engine.py
──────────────────────
TrackingEngine: the single-process tracking state machine.

Responsibilities (architecture §5/§31):
  • Ingest every parsed packet from the acquisition service (one call, no
    changes to the serial reader loop beyond that).
  • Arbitrate the position-source hierarchy: GPS > ORB > TLE/SGP4 > none.
    Sources are NEVER silently blended; every state carries its source and
    its age so the UI can always tell the truth.
  • Compute ground-station geometry (WGS84 topocentric) for whichever
    source is authoritative.
  • Maintain the predicted-track cache (recomputed on TLE change, or when
    older than TRACK_REFRESH_S).
  • Compute pass predictions when a TLE becomes active/refreshed; persist
    them and expose the next pass in every state broadcast.
  • Broadcast a 1 Hz `tracking` SSE event with the full tracking state.

Thread model: one daemon ticker thread at 1 Hz; ingest() is called from the
serial reader thread.  All shared state is guarded by a single re-entrant
lock; SSE broadcast goes through the existing ClientRegistry (drop-oldest,
never blocks the reader).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from tansat import db as tansat_db
from tansat.orbit import coords, passes, propagator, tles, tracks

logger = logging.getLogger(__name__)

TICK_INTERVAL_S   = 1.0
TRACK_REFRESH_S   = 60.0     # predicted-track cache lifetime
PASS_HORIZON_H    = 48.0
FIX_PERSIST_EVERY = 5        # persist every Nth fix (position_fixes is a
                             # sample, not a full copy of the packet stream)
ORBIT_INJECT_WINDOW_S = 3600.0   # GPS-vs-SGP4 error sampled within 60 min


class TrackingEngine:
    def __init__(self, data_dir: str = "data"):
        self._lock = threading.RLock()
        self._gs_lat, self._gs_lon, self._gs_alt = -6.813737, 39.280427, 30.0
        self._min_el = 0.0

        # Source states (last accepted fix per source)
        self._gps_fix: dict | None = None     # {lat, lon, alt_m, raw_lat, raw_lon, t, ts}
        self._orb_fix: dict | None = None
        self._last_persist = {"gps": 0, "orb": 0}

        # TLE / propagation state
        self._satrec = None
        self._tle_meta: dict | None = None
        self._track_cache: dict | None = None
        self._track_cache_at = 0.0
        self._passes_cache: list[dict] = []
        self._next_pass: dict | None = None
        self._passes_computed_for: int | None = None   # tle id

        # ticker
        self._ticker = None
        self._running = False

    # ── Configuration ────────────────────────────────────────────────
    def update_gs_config(self, gs_lat: float, gs_lon: float, gs_alt: float,
                         alt_offset: float = 0.0) -> None:
        with self._lock:
            self._gs_lat, self._gs_lon, self._gs_alt = gs_lat, gs_lon, gs_alt
            self._track_cache = None        # geometry changed → invalidate
            self._next_pass = None
            self._passes_computed_for = None
            try:
                self._min_el = float(tansat_db.get_setting("min_elevation_deg", 0.0))
            except Exception:
                self._min_el = 0.0

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._load_active_tle()
            self._ticker = threading.Thread(target=self._tick_loop,
                                            name="tracking-engine", daemon=True)
            self._ticker.start()
            logger.info("TrackingEngine started")

    def stop(self) -> None:
        with self._lock:
            self._running = False

    # ── TLE management ───────────────────────────────────────────────
    def import_tle(self, text: str, source: str, actor: str | None = None) -> dict:
        result = tles.import_tle(text, source, actor=actor)
        self._load_active_tle(force_reload=True)
        return result

    def _load_active_tle(self, force_reload: bool = False) -> None:
        with self._lock:
            rec = None
            try:
                rec = tles.get_active_tle()
            except Exception as e:
                logger.error("TLE load failed: %s", e)
                return
            if not rec:
                self._satrec = None
                self._tle_meta = None
                return
            if not force_reload and self._tle_meta and self._tle_meta["id"] == rec["id"]:
                return
            try:
                self._satrec = propagator.make_satrec(rec["line1"], rec["line2"])
            except propagator.PropagationError as e:
                logger.error("Active TLE rejected by SGP4: %s", e)
                self._satrec = None
                self._tle_meta = None
                return
            self._tle_meta = rec
            self._track_cache = None
            self._next_pass = None
            self._passes_computed_for = None
            logger.info("Tracking TLE active: NORAD %s epoch %s (age %.1f h)",
                        rec["norad_id"], rec["epoch"].isoformat(), rec["age_h"])

    # ── Packet ingestion ─────────────────────────────────────────────
    def ingest(self, pkt: dict) -> None:
        """Called by the acquisition reader for every parsed packet."""
        ptype = (pkt.get("type") or "").upper()
        if ptype == "TLM":
            self._ingest_tlm(pkt)
        elif ptype == "ORB":
            self._ingest_orb(pkt)

    def _ingest_tlm(self, pkt: dict) -> None:
        # Prefer RAW lat/lon for science; fall back to filtered.
        raw_lat = pkt.get("raw_latitude")
        raw_lon = pkt.get("raw_longitude")
        lat = pkt.get("latitude")
        lon = pkt.get("longitude")
        if lat in (None, 0.0) or lon in (None, 0.0):
            return                      # no-fix: never fabricate a position
        now = datetime.now(timezone.utc)
        with self._lock:
            self._gps_fix = {
                "lat": float(lat), "lon": float(lon),
                "raw_lat": float(raw_lat) if raw_lat is not None else None,
                "raw_lon": float(raw_lon) if raw_lon is not None else None,
                "alt_m": pkt.get("true_altitude_m"),
                "az": pkt.get("azimuth"), "el": pkt.get("elevation"),
                "slant_km": pkt.get("slant_km"),
                "range_rate_km_s": pkt.get("range_rate_km_s"),
                "t": now,
                "server_ts": pkt.get("server_ts") or now.isoformat(),
            }

    # Persist sampled fixes (INSERT every FIX_PERSIST_EVERY) — position_fixes
    # is a sampled scientific record, not a packet mirror.
    def _maybe_persist_fix(self, source: str, fix: dict) -> None:
        now = time.monotonic()
        if now - self._last_persist.get(source, 0.0) < FIX_PERSIST_EVERY:
            return
        self._last_persist[source] = now
        try:
            sat = tles.get_primary_satellite()
            sid = sat["id"] if sat else None
            with tansat_db.get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO position_fixes (satellite_id, fix_time, server_time, "
                    "source, lat, lon, alt_m, raw_lat, raw_lon, az, el, slant_km, "
                    "range_rate_km_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sid, fix["t"], fix.get("server_ts") or fix["t"], source,
                     fix["lat"], fix["lon"], fix.get("alt_m"),
                     fix.get("raw_lat"), fix.get("raw_lon"),
                     fix.get("az"), fix.get("el"), fix.get("slant_km"),
                     fix.get("range_rate_km_s")))
                conn.commit()
        except Exception as e:
            logger.warning("position_fixes insert failed: %s", e)

    def _ingest_orb(self, pkt: dict) -> None:
        lat = pkt.get("latitude")
        lon = pkt.get("longitude")
        if lat in (None, 0.0) or lon in (None, 0.0):
            return
        now = datetime.now(timezone.utc)
        with self._lock:
            self._orb_fix = {
                "lat": float(lat), "lon": float(lon),
                "alt_m": pkt.get("true_altitude_m"),
                "az": pkt.get("azimuth"), "el": pkt.get("elevation"),
                "slant_km": pkt.get("slant_km"),
                "range_rate_km_s": pkt.get("range_rate_km_s"),
                "next_aos": pkt.get("next_aos"),
                "max_elevation": pkt.get("max_elevation"),
                "t": now,
                "server_ts": now.isoformat(),
            }
        self._maybe_persist_fix("orb", self._orb_fix)

    # ── Source hierarchy (§12): the ONE place the decision lives ─────
    def _arbitrate(self) -> tuple[dict | None, str]:
        now = datetime.now(timezone.utc)
        gps_fresh = (self._gps_fix is not None
                     and (now - self._gps_fix["t"]).total_seconds() <= tles.GPS_FRESH_S)
        orb_fresh = (self._orb_fix is not None
                     and (now - self._orb_fix["t"]).total_seconds() <= tles.ORB_FRESH_S)
        if gps_fresh:
            return self._gps_fix, "gps"
        if orb_fresh:
            return self._orb_fix, "orb"
        if (self._satrec is not None and self._tle_meta
                and tles.tle_usable(self._tle_meta["epoch"])):
            return None, "tle"
        return None, "none"      # expired TLE is NOT a position source (§11)

    # ── Ticker ───────────────────────────────────────────────────────
    def _tick_loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as e:
                logger.exception("Tracking tick failed: %s", e)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.05, TICK_INTERVAL_S - elapsed))

    def _tick(self) -> None:
        with self._lock:
            now = datetime.now(timezone.utc)
            fix, source = self._arbitrate()

            # Ensure the pass schedule exists for the active TLE (computed
            # once per TLE / station config; invalidated elsewhere).  Done in
            # the ticker so every SSE state carries next_pass without any
            # client having to call /api/tracking/passes first.
            if (self._satrec is not None and self._tle_meta is not None
                    and tles.tle_usable(self._tle_meta["epoch"])
                    and self._passes_computed_for != self._tle_meta["id"]):
                self._compute_passes(PASS_HORIZON_H)

            state = self._build_state(now, fix, source)
            if fix is not None:
                self._maybe_persist_fix(source, fix)
            # Always include the propagated state: it drives the 3D inertial
            # rendering and the GPS-vs-model error, independent of which
            # source currently wins the display arbitration.
            if self._satrec is not None and (source == "tle"
                                             or fix is not None):
                tle_state = self._propagated_state(now)
                if tle_state:
                    state["predicted_position"] = tle_state
            self._broadcast(state)

    def _propagated_state(self, now: datetime) -> dict | None:
        """Current SGP4 position + GS geometry (used when TLE is the source,
        and as the prediction-vs-actual comparison whenever GPS/ORB is live)."""
        try:
            r = propagator.propagate(self._satrec, now)
        except propagator.PropagationError:
            return None
        sat_ecef = coords.ecef_from_geodetic(r["lat_deg"], r["lon_deg"], r["alt_m"])
        geo = coords.topocentric_az_el_range(self._gs_lat, self._gs_lon,
                                             self._gs_alt, sat_ecef, r["v_ecef_mps"])
        return {
            "lat": r["lat_deg"], "lon": r["lon_deg"], "alt_km": r["alt_m"] / 1000.0,
            "az": geo["azimuth_deg"], "el": geo["elevation_deg"],
            "range_km": geo["range_m"] / 1000.0,
            "range_rate_km_s": (geo["range_rate_mps"] / 1000.0
                                if geo["range_rate_mps"] is not None else None),
            "speed_km_s": r["speed_mps"] / 1000.0,
            # TEME position (km) + GMST — lets the 3D view place the satellite
            # in the INERTIAL frame and rotate the Earth to its true
            # orientation instead of re-deriving anything client-side.
            "eci_km": [r["r_teme_m"][0] / 1000.0,
                       r["r_teme_m"][1] / 1000.0,
                       r["r_teme_m"][2] / 1000.0],
            "gmst_deg": coords.gmst_deg(now),
        }

    def _build_state(self, now, fix, source) -> dict:
        state = {
            "generated_at": now.isoformat(),
            "source": source,
            "satellite": (self._tle_meta["name"] if self._tle_meta else None),
            "norad_id": (self._tle_meta["norad_id"] if self._tle_meta else None),
            "tle": self._tle_brief(),
        }
        if fix is not None:
            state["position"] = {
                "lat": fix["lat"], "lon": fix["lon"],
                "alt_m": fix.get("alt_m"),
                "utc": fix["t"].isoformat(),
                "age_s": (now - fix["t"]).total_seconds(),
            }
            # Ground-station geometry recomputed WGS84-correctly from the fix.
            alt_m = fix.get("alt_m") or 0.0
            sat_ecef = coords.ecef_from_geodetic(fix["lat"], fix["lon"], alt_m)
            geo = coords.topocentric_az_el_range(self._gs_lat, self._gs_lon,
                                                 self._gs_alt, sat_ecef)
            state["geometry"] = {
                "az": geo["azimuth_deg"], "el": geo["elevation_deg"],
                "range_km": geo["range_m"] / 1000.0,
            }
            state["visible"] = geo["elevation_deg"] >= self._min_el
            # GPS-vs-SGP4 comparison whenever both exist (§20/§38)
            if self._satrec is not None:
                pred = self._propagated_state(now)
                if pred:
                    state["gp_error"] = self._error_metrics(fix, pred)
        else:
            state["position"] = None
            state["geometry"] = None
            state["visible"] = False
        if source == "tle" and self._satrec is not None:
            pred = self._propagated_state(now)
            if pred:
                state["position"] = {
                    "lat": pred["lat"], "lon": pred["lon"],
                    "alt_m": pred["alt_km"] * 1000.0,
                    "utc": now.isoformat(), "age_s": 0.0,
                }
                state["geometry"] = {
                    "az": pred["az"], "el": pred["el"],
                    "range_km": pred["range_km"],
                }
                state["visible"] = pred["el"] >= self._min_el
        state["next_pass"] = self._next_pass_brief()
        state["footprint"] = None
        if state["position"]:
            state["footprint"] = tracks.coverage_footprint(
                state["position"]["lat"], state["position"]["lon"],
                state["position"]["alt_m"] or 0.0, self._min_el)
        return state

    def _error_metrics(self, fix: dict, pred: dict) -> dict:
        """GPS/ORB-vs-SGP4 comparison (architecture §20)."""
        import math
        ground_km = coords.haversine_km(fix["lat"], fix["lon"],
                                        pred["lat"], pred["lon"])
        dalt_m = (fix.get("alt_m") or 0.0) - pred["alt_km"] * 1000.0
        return {
            "ground_km": round(ground_km, 3),
            "alt_m": round(dalt_m, 1),
            "pred_lat": pred["lat"], "pred_lon": pred["lon"],
        }

    def _tle_brief(self) -> dict | None:
        if not self._tle_meta:
            return None
        m = self._tle_meta
        return {
            "id": m["id"], "norad_id": m["norad_id"],
            "epoch": m["epoch"].isoformat(), "age_h": round(m["age_h"], 2),
            "status": m["status"], "source": m["source"],
        }

    # ── Track cache ──────────────────────────────────────────────────
    def get_predicted_track(self, force: bool = False) -> dict | None:
        with self._lock:
            now_m = time.monotonic()
            if (self._track_cache is None or force
                    or now_m - self._track_cache_at > TRACK_REFRESH_S):
                if self._satrec is None or self._tle_meta is None:
                    return None
                if not tles.tle_usable(self._tle_meta["epoch"]):
                    return None        # expired TLE: withdraw the prediction
                try:
                    self._track_cache = tracks.generate_predicted_track(
                        self._satrec, gs_lat=self._gs_lat, gs_lon=self._gs_lon,
                        gs_alt_m=self._gs_alt)
                    self._track_cache_at = now_m
                except propagator.PropagationError as e:
                    logger.error("Track generation failed: %s", e)
                    return None
            cache = self._track_cache
        # Serialize outside the lock
        return {
            "generated_at": cache["generated_at"],
            "points": [
                {"utc": p["utc"].isoformat(), "lat": p["lat"], "lon": p["lon"],
                 "alt_m": p["alt_m"],
                 "el": p.get("elevation_deg"),
                 "az": p.get("azimuth_deg"),
                 "in_future": p["in_future"]}
                for p in cache["points"]
            ],
            "future_segments": cache["future_segments"],
            "past_segments": cache["past_segments"],
        }

    # ── Pass prediction ──────────────────────────────────────────────
    def get_passes(self, hours: float = PASS_HORIZON_H,
                   force: bool = False) -> list[dict]:
        with self._lock:
            if self._satrec is None or self._tle_meta is None:
                return []
            if not tles.tle_usable(self._tle_meta["epoch"]):
                return []      # expired TLE: no model products (same rule as tracks)
            if (force or self._passes_computed_for != self._tle_meta["id"]
                    or self._next_pass is None):
                self._compute_passes(hours)
            plist = self._passes_cache
        return [
            {"aos": p["aos_utc"].isoformat(),
             "tca": p["tca_utc"].isoformat() if p["tca_utc"] else None,
             "los": p["los_utc"].isoformat() if p["los_utc"] else None,
             "max_elevation_deg": p["max_elevation_deg"],
             "aos_azimuth_deg": p["aos_azimuth_deg"],
             "los_azimuth_deg": p["los_azimuth_deg"],
             "duration_s": p["duration_s"],
             "tca_range_km": p["tca_range_km"]}
            for p in plist
        ]

    def _compute_passes(self, hours: float) -> None:
        try:
            plist = passes.find_passes(
                self._satrec, self._gs_lat, self._gs_lon, self._gs_alt,
                hours=hours, min_elevation_deg=self._min_el)
        except Exception as e:
            logger.error("Pass prediction failed: %s", e)
            self._passes_cache = []
            return
        self._passes_cache = plist
        self._passes_computed_for = self._tle_meta["id"]
        self._next_pass = passes.next_pass(plist)
        # Persist for the pass-schedule table (best effort)
        try:
            sat = tles.get_primary_satellite()
            if sat:
                with tansat_db.get_conn() as conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM pass_predictions WHERE satellite_id = %s",
                                (sat["id"],))
                    for p in plist:
                        cur.execute(
                            "INSERT INTO pass_predictions (satellite_id, tle_id, aos, tca, "
                            "los, max_elevation, aos_azimuth, los_azimuth, duration_s, "
                            "min_elevation_mask) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (sat["id"], self._tle_meta["id"], p["aos_utc"], p["tca_utc"],
                             p["los_utc"], p["max_elevation_deg"], p["aos_azimuth_deg"],
                             p["los_azimuth_deg"], p["duration_s"], self._min_el))
                    conn.commit()
        except Exception as e:
            logger.warning("pass_predictions persist failed: %s", e)

    def _next_pass_brief(self) -> dict | None:
        np_ = self._next_pass
        if not np_:
            return None
        return {
            "aos": np_["aos_utc"].isoformat(),
            "tca": np_["tca_utc"].isoformat() if np_["tca_utc"] else None,
            "los": np_["los_utc"].isoformat() if np_["los_utc"] else None,
            "max_elevation_deg": np_["max_elevation_deg"],
            "aos_azimuth_deg": np_["aos_azimuth_deg"],
            "los_azimuth_deg": np_["los_azimuth_deg"],
            "duration_s": np_["duration_s"],
        }

    # ── SSE broadcast ────────────────────────────────────────────────
    def _broadcast(self, state: dict) -> None:
        try:
            from tansat import acquisition as acq
            svc = acq.get_service()
            svc.clients.broadcast("tracking", state)
        except RuntimeError:
            pass    # service not initialized (unit tests) — skip silently

    def get_state(self) -> dict:
        with self._lock:
            now = datetime.now(timezone.utc)
            fix, source = self._arbitrate()
            state = self._build_state(now, fix, source)
            return json_safe(state)


def _json_safe_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Not JSON serializable: {o!r}")


# Convenience for routes: convert datetimes defensively
def json_safe(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
