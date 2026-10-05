"""
tansat/orbit
────────────
Professional orbital tracking subsystem for the TANZANITESAT ground station.

Modules
-------
coords       Reference frames: GMST, TEME→ECEF, WGS84 geodetic, topocentric Az/El.
tles         TLE validation, epoch parsing, staleness policy, persistence.
propagator   SGP4 wrapper (single + vectorized batch propagation).
tracks       Predicted/observed ground-track generation, dateline splitting.
passes       AOS/LOS/TCA/max-elevation pass prediction.
engine       TrackingEngine: source arbitration, 1 Hz ticker, SSE fan-out.

Architecture rule (mirrors the rest of tansat/): **no web-framework imports**.
The Flask routes consume these modules through the TrackingEngine API only.
"""

__all__ = ["coords", "tles", "propagator", "tracks", "passes", "engine"]
