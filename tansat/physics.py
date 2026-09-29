from math import radians, degrees, atan2, sqrt, cos, sin, asin

EARTH_RADIUS_M = 6_371_000.0
EARTH_RADIUS_KM = 6_371.0

# §3.1 Pointing geometry
def calculate_azimuth_and_range(gs_lat, gs_lon, gs_alt, dev_lat, dev_lon, dev_alt):
    """Returns (azimuth_deg, horizontal_m, slant_m, elevation_deg).
    gs_alt and dev_alt both in metres MSL.
    """
    lat1, lat2 = radians(gs_lat), radians(dev_lat)
    dlat, dlon = radians(dev_lat - gs_lat), radians(dev_lon - gs_lon)

    y = sin(dlon) * cos(lat2)
    x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    az = (degrees(atan2(y, x)) + 360.0) % 360.0

    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    a = max(0.0, min(1.0, a))
    horizontal = 2 * EARTH_RADIUS_M * atan2(sqrt(a), sqrt(1 - a))

    d_alt = dev_alt - gs_alt
    slant = sqrt(horizontal**2 + d_alt**2)
    elevation = (degrees(atan2(d_alt, horizontal)) if horizontal > 1e-6
                 else (90.0 if d_alt > 0 else (-90.0 if d_alt < 0 else 0.0)))
    return az, horizontal, slant, elevation

# §3.2 Attitude from accelerometer
def accel_orientation(ax, ay, az):
    """Returns (pitch_deg, roll_deg) from body-frame accelerometer (in g)."""
    pitch = degrees(atan2(ax, sqrt(ay**2 + az**2)))
    roll  = degrees(atan2(ay, sqrt(ax**2 + az**2)))
    return pitch, roll

# §3.3 Gyro dead-band
GYRO_DEADBAND = 0.05  # °/s
def apply_gyro_deadband(rate):
    """Clamp gyro rates with |rate| < 0.05 °/s to exactly zero."""
    return 0.0 if abs(rate) < GYRO_DEADBAND else rate

def _wrap180(angle):
    """Normalize an angle to (-180, 180]."""
    angle = angle % 360.0
    if angle > 180.0:
        angle -= 360.0
    return angle

# §3.4 Complementary attitude filter — alpha and gyro scale are runtime-tunable
# via the admin settings page (keys: attitude_alpha, gyro_scale).
ATTITUDE_ALPHA = 0.98     # accel weight per packet (lower = more responsive)
GYRO_SCALE   = 1.0        # firmware gyro units → °/s multiplier
# dt clamp: packets arrive seconds apart on this hardware (observed 2.5–10 s).
# The old 0.5 s DT_MAX silently discarded ~80% of every rotation; a generous
# upper bound still guards against garbage dt after long dropouts/reconnects.
DT_MIN, DT_MAX = 0.005, 30.0
# Motion gating: the accelerometer only yields a valid gravity reference when
# it measures ~1 g. While the device is accelerating or being handled, |a|
# leaves that band and the accel-derived pitch/roll is noise — blending it in
# makes the attitude crawl toward garbage angles after every rotation.
ACCEL_NORM_TOL = 0.35        # |a| must be within ±0.35 g of 1 g to trust accel
MOTION_GYRO_THRESHOLD = 0.15  # °/s (post-scale): above this the body is rotating
# Packet interval the alpha convergence table assumes (spec's 50 Hz telemetry).
# The accel blend is scaled by dt/ATTITUDE_DT_REF so convergence takes the same
# WALL-CLOCK time at any packet rate — at 2 Hz a plain alpha=0.98 blend would
# otherwise need ~25 s to re-anchor instead of ~1 s.
ATTITUDE_DT_REF = 0.02

# ── Gyro bias estimation ─────────────────────────────────────────────
# Real IMUs have a nonzero stationary output (observed ~0.02–0.03 rad/s ≈
# 1.1–1.7 °/s after unit conversion), which dead-reckons into ~70–100°/min of
# phantom rotation when the satellite parks. The filter estimates the bias
# from still samples and subtracts it before integrating.
BIAS_WARMUP_SAMPLES   = 6    # packets to average before integrating gyro
BIAS_WARMUP_MAX_RATE  = 5.0   # °/s post-scale: still enough for warmup. Must sit
                              # ABOVE the gyro's own bias (~1.1–1.7 °/s observed)
                              # but far below any hand rotation (≥20 °/s).
BIAS_WARMUP_MAX_ATTEMPTS = 30 # give up after this many packets: integrate
                              # uncalibrated (bias 0) rather than freeze forever
BIAS_EMA_ALPHA        = 0.02  # EMA weight at the 50 Hz reference rate; rate-
                              # normalized at runtime (see _bias_ema_alpha)
BIAS_EMA_MAX_STEP     = 0.25  # cap: one packet gap can never adapt bias fully
BIAS_HOLDOUT_SECONDS  = 5.0   # skip bias updates this long after motion stops

class AttitudeFilter:
    def __init__(self, alpha: float = ATTITUDE_ALPHA,
                 gyro_scale: float = GYRO_SCALE):
        """
        alpha:      accelerometer weight per packet; convergence ≈ 1/(1-alpha)
                    packets (0.98 → ~50, 0.95 → ~20, 0.90 → ~10).
        gyro_scale: multiplier converting firmware gyro units to °/s
                    (e.g. raw 16-bit IMU at ±1000 dps → 0.0305).
        """
        self.alpha = max(0.0, min(1.0, float(alpha)))
        self.gyro_scale = float(gyro_scale)
        self.pitch = 0.0
        self.roll  = 0.0
        self.yaw   = 0.0
        self._first = True
        # Gyro-bias estimator state
        self._bias = [0.0, 0.0, 0.0]      # °/s, subtracted before integration
        self._warmup = []                 # pre-calibration still samples
        self._warmup_attempts = 0         # packets seen during warmup phase
        self.bias_ready = False           # True once warmup produced a bias
        self._last_motion_ts = None       # monotonic-ish timestamp (s of integration time)
        self._motion_time = 0.0           # cumulative dt while gyro is active

    def _gyro_still(self, gx, gy, gz) -> bool:
        """True if all raw (scaled) gyro axes are below the still threshold."""
        return max(abs(gx), abs(gy), abs(gz)) <= BIAS_WARMUP_MAX_RATE

    def _blend_to_accel(self, pitch_acc, roll_acc, gx, gy, dt):
        """Rate-normalized accel blend (see ATTITUDE_DT_REF). Identical to the
        spec formula pitch = α·(pitch + gx·dt) + (1-α)·pitch_acc at 50 Hz, but
        converges in the same number of *seconds* (not packets) at low rates.
        The blend is capped so a multi-second packet gap can never fully
        override the gyro integration over that interval (50/50 worst case)."""
        blend = (1.0 - self.alpha) * (dt / ATTITUDE_DT_REF)
        blend = max(0.0, min(0.5, blend))
        keep = 1.0 - blend
        self.pitch = _wrap180(keep * (self.pitch + gx * dt) + blend * pitch_acc)
        self.roll  = _wrap180(keep * (self.roll  + gy * dt) + blend * roll_acc)

    def _bias_ema_alpha(self, dt) -> float:
        """EMA weight for this packet's dt: converges in the same WALL-CLOCK
        time at any packet rate (at 50 Hz it equals BIAS_EMA_ALPHA), capped
        so a single multi-second gap cannot snap the bias estimate."""
        a = 1.0 - (1.0 - BIAS_EMA_ALPHA) ** (dt / ATTITUDE_DT_REF)
        return max(0.0, min(BIAS_EMA_MAX_STEP, a))

    def update(self, ax, ay, az, gx, gy, gz, dt):
        # Scale firmware gyro units → °/s. Dead-band is applied AFTER bias
        # removal so a constant bias is never masked by the dead-band.
        gx = gx * self.gyro_scale
        gy = gy * self.gyro_scale
        gz = gz * self.gyro_scale
        dt = max(DT_MIN, min(DT_MAX, dt))
        pitch_acc, roll_acc = accel_orientation(ax, ay, az)

        # ── Bias warmup: average the first still samples to seed the bias.
        # Until calibrated (or the attempt cap gives up), track accel only so
        # the bias never leaks into the pose.
        if not self.bias_ready and self._warmup_attempts < BIAS_WARMUP_MAX_ATTEMPTS:
            self._warmup_attempts += 1
            if self._gyro_still(gx, gy, gz):
                self._warmup.append([gx, gy, gz])
                if len(self._warmup) >= BIAS_WARMUP_SAMPLES:
                    self._bias = [sum(c) / len(self._warmup)
                                  for c in zip(*self._warmup)]
                    self.bias_ready = True
            if self._first:
                self.pitch = pitch_acc
                self.roll  = roll_acc
                self._first = False
            else:
                self._blend_to_accel(pitch_acc, roll_acc, 0.0, 0.0, dt)
            return self.pitch, self.roll, self.yaw

        # Raw (post-scale) rates kept for the bias estimator: it must see the
        # PRE-deadband residual, otherwise it can never track a bias smaller
        # than the dead-band itself.
        raw = (gx, gy, gz)

        # ── Slow bias re-estimation while parked. Stillness classification
        # uses the RAW signal, NOT the bias-corrected residual: if the bias
        # estimate is off by more than MOTION_GYRO_THRESHOLD, the corrected
        # residual would look like permanent "motion", locking the estimator
        # out forever while the pose integrates a phantom rate (observed on
        # real hardware as an exact constant −1.72 °/s roll drift). A holdout
        # after genuine (raw) motion still protects against absorbing a real
        # rotation's decaying tail into the bias.
        self._motion_time += dt
        if not self._gyro_still(*raw):
            self._last_motion_ts = self._motion_time
        elif (self._motion_time - (self._last_motion_ts or 0.0)
              >= BIAS_HOLDOUT_SECONDS):
            # Parked: the raw reading IS the current bias — track it slowly.
            a = self._bias_ema_alpha(dt)
            self._bias = [b + a * (r - b)
                          for b, r in zip(self._bias, raw)]

        # Remove estimated bias, then apply the small-signal dead-band.
        gx = apply_gyro_deadband(raw[0] - self._bias[0])
        gy = apply_gyro_deadband(raw[1] - self._bias[1])
        gz = apply_gyro_deadband(raw[2] - self._bias[2])

        a_norm = sqrt(ax * ax + ay * ay + az * az)
        accel_invalid = abs(a_norm - 1.0) > ACCEL_NORM_TOL
        gyro_active   = max(abs(gx), abs(gy), abs(gz)) > MOTION_GYRO_THRESHOLD

        # Motion gate: coast on gyro integration while the accel reference is
        # implausible (|a| outside the 1 g band) or the body is rotating. Once
        # the device rests on a plausible gravity vector, resume the blend so
        # the attitude re-anchors instead of drifting.
        if accel_invalid or gyro_active:
            self.pitch = _wrap180(self.pitch + gx * dt)
            self.roll  = _wrap180(self.roll  + gy * dt)
        else:
            self._blend_to_accel(pitch_acc, roll_acc, gx, gy, dt)
        self.yaw = _wrap180(self.yaw + gz * dt)
        return self.pitch, self.roll, self.yaw

    def reset(self):
        self.__init__()

# §3.5 GPS jitter filter
GPS_DEADBAND_M = 2.0
GPS_EMA_ALPHA  = 0.2

class GpsFilter:
    def __init__(self):
        self._lat = None
        self._lon = None
        self._valid = False

    def update(self, lat, lon):
        """Returns (filtered_lat, filtered_lon, is_valid)."""
        if lat == 0.0 or lon == 0.0 or lat is None or lon is None:
            return self._lat, self._lon, False
        if self._lat is None:
            self._lat, self._lon = lat, lon
            self._valid = True
            return lat, lon, True
        # haversine distance
        from math import radians, sin, cos, sqrt, atan2
        R = EARTH_RADIUS_M
        lat1, lat2 = radians(self._lat), radians(lat)
        dlat = radians(lat - self._lat)
        dlon = radians(lon - self._lon)
        a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
        a = max(0.0, min(1.0, a))
        dist = 2 * R * atan2(sqrt(a), sqrt(1 - a))
        if dist <= GPS_DEADBAND_M:
            return self._lat, self._lon, True
        self._lat = GPS_EMA_ALPHA * lat + (1 - GPS_EMA_ALPHA) * self._lat
        self._lon = GPS_EMA_ALPHA * lon + (1 - GPS_EMA_ALPHA) * self._lon
        self._valid = True
        return self._lat, self._lon, True

    def reset(self):
        self._lat = self._lon = None
        self._valid = False

# §3.6 Range-rate and Doppler
RANGE_RATE_EMA_ALPHA = 0.3
SPEED_OF_LIGHT = 299_792_458.0  # m/s

class RangeRateFilter:
    def __init__(self):
        self._prev_slant_m = None
        self._prev_time    = None
        self._rate_ema     = 0.0

    def update(self, slant_m, now, gps_lock):
        """Returns (range_rate_km_s, doppler_100mhz) or (None, None) if not enough data."""
        if not gps_lock:
            self.reset()
            return None, None
        if self._prev_slant_m is None:
            self._prev_slant_m = slant_m
            self._prev_time    = now
            return None, None
        dt = now - self._prev_time
        if 0 < dt <= 5.0:
            raw_rate = (slant_m - self._prev_slant_m) / dt
            self._rate_ema = RANGE_RATE_EMA_ALPHA * raw_rate + (1 - RANGE_RATE_EMA_ALPHA) * self._rate_ema
            range_rate_km_s = self._rate_ema / 1000.0
            doppler_100mhz  = -(self._rate_ema * 100e6 / SPEED_OF_LIGHT)
        else:
            range_rate_km_s = None
            doppler_100mhz  = None
        self._prev_slant_m = slant_m
        self._prev_time    = now
        return range_rate_km_s, doppler_100mhz

    def reset(self):
        self._prev_slant_m = None
        self._prev_time    = None
        self._rate_ema     = 0.0

# §3.7 Battery percentage (non-linear Li-ion curve)
BATTERY_CURVE = [
    (3.30, 0.0),
    (3.65, 15.0),
    (3.80, 40.0),
    (3.90, 62.0),
    (4.05, 82.0),
    (4.20, 100.0),
]

def battery_percent(voltage):
    """Piecewise-interpolate voltage to percent. If voltage > 5.0, assume it's already a percentage."""
    if voltage > 5.0:
        return max(0.0, min(100.0, voltage))
    v = max(BATTERY_CURVE[0][0], min(BATTERY_CURVE[-1][0], voltage))
    for i in range(len(BATTERY_CURVE) - 1):
        v0, p0 = BATTERY_CURVE[i]
        v1, p1 = BATTERY_CURVE[i + 1]
        if v0 <= v <= v1:
            t = (v - v0) / (v1 - v0)
            return max(0.0, min(100.0, p0 + t * (p1 - p0)))
    return 0.0
