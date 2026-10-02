from collections import deque
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
ACCEL_REST_BAND_FRAC = 0.15  # rest-baseline band: |a| within ±15% of rest
ACCEL_BASELINE_PACKETS = 5   # calm packets needed to (re)learn the rest |a|
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
BIAS_WARMUP_MAX_RATE  = 12.0  # °/s post-scale: still enough for warmup. Must sit
                              # ABOVE the gyro's own bias — observed ~1.1–1.7 °/s
                              # on the bench unit but ~7.4 °/s on the flight unit
                              # (-0.13 rad/s firmware × 57.2958) — and far below
                              # any hand rotation (~57 °/s observed).
BIAS_WARMUP_MAX_ATTEMPTS = 30 # give up after this many packets: integrate
                              # uncalibrated (bias 0) rather than freeze forever
BIAS_EMA_ALPHA        = 0.02  # EMA weight at the 50 Hz reference rate; rate-
                              # normalized at runtime (see _bias_ema_alpha)
BIAS_EMA_MAX_STEP     = 0.25  # cap: one packet gap can never adapt bias fully
BIAS_HOLDOUT_SECONDS  = 5.0   # skip bias updates this long after motion stops

# ── Frozen-packet glitch override (ground-station MPU6050 lockup fix) ──
# When the CubeSat is rotated very fast, the onboard MPU6050 FIFO buffer/I2C
# bus can lock up: the firmware keeps re-broadcasting the SAME gyro sample
# (observed frozen at (0.97, -1.06) and at (-1.09, 0.05, -0.96) rad/s) while
# the body is already stationary on the bench. Integrating that static
# "rate" is the Gyroscope-Runaway defect. Firmware cannot be reflashed, so
# the ground station must detect it here, at packet-processing time.
# Real MEMS stillness always carries floating decimal micro-noise, so N
# EXACTLY identical consecutive samples is a locked buffer, not a measurement.
FROZEN_GYRO_PACKETS = 3       # identical consecutive packets → stream FROZEN
# A frozen dump only needs suppressing when the value could actually move the
# pose. Genuine stillness can also quantize to identical samples at firmware
# precision (observed: a parked gyro repeats (0.02, 0.00, -0.03) for packet
# runs) — those are harmless, and the bias estimator MUST keep seeing them
# (it calibrates on them), so only LARGE frozen values are a glitch. The
# ceiling is the stillness threshold: no parked sensor legitimately reads
# above it, while real rotation never repeats bit-exactly for 3 packets.
FROZEN_GLITCH_MIN_RATE = BIAS_WARMUP_MAX_RATE   # °/s post-scale

# ── Accelerometer gravity fallback (static-accel veto) ──────────────
# During the lockup the accelerometer keeps reporting the true resting
# gravity vector (near-zero variance between packets) while the gyro claims
# a violent rotation — impossible physics. When the accel window is static
# and the (cleaned) gyro still claims rotation, the gyro's fusion weight
# drops to 0 and pitch/roll are taken PURELY from the accelerometer gravity
# vectors. Yaw has no accel anchor (rotation about the gravity axis is
# invisible to the accelerometer), so gz keeps integrating — a real yaw spin
# must still track; a glitched gz is already forced to 0 by the detector.
ACCEL_STATIC_WINDOW   = 4      # packets in the accel variance window
ACCEL_STATIC_REL_TOL  = 0.10   # max component drift ≤ 10% of mean |a|
ACCEL_STATIC_MIN_NORM = 1e-3   # below this |a| there is no usable gravity vector

# ── Frozen-buffer veto (gyro-vs-accel contradiction) ─────────────────
# Fires the moment gx or gy claims rotation while the accelerometer still
# reads the rest vector — impossible physics for pitch/roll, and at this
# packet rate a single integrated phantom packet is ~165 deg of error
# (frozen -1.09 rad/s × 3 s). See AttitudeFilter.update for the per-axis
# reasoning that keeps genuine yaw spins integrating.
GYRO_CONTRADICTION_PACKETS = 1  # kept for ops reference/diagnostics

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
        # ── Accel baseline (rest-vector calibration) ─────────────────
        # Real hardware may deliver accel in a non-1g scale (observed: rest
        # vector (5.75, -22.09, 19.22) → |a| ≈ 29.9 "g"). The motion gate
        # therefore compares |a| against the LEARNED REST MAGNITUDE, not an
        # absolute 1 g — this makes the gate scale-invariant and re-arms the
        # accelerometer as a usable gravity reference.
        self._accel_rest_mag = None       # learned |a| while parked (None = unknown)
        self._accel_rest_axis = None      # learned gravity direction (unit vector)
        self._accel_still_run = 0         # consecutive accel-calm packets
        # ── Stale-gyro detector state (kept for diagnostics only) ────
        self._last_raw_gyro = None        # previous raw (scaled) tuple
        self._contradiction_run = 0       # consecutive gyro-vs-accel contradictions
        # ── Frozen-packet glitch detector (MPU6050 I2C/FIFO lockup) ──
        self._prev_gyro = None            # previous scaled (gx, gy, gz) triple
        self._gyro_repeat_run = 0         # consecutive exactly-identical packets (−1)
        self.frozen_glitch = False        # True while a frozen dump is suppressed
        self.frozen_streak = 0            # identical-packet streak length (diagnostic)
        # ── Accelerometer static window (gravity-fallback gate) ──────
        self._accel_window = deque(maxlen=ACCEL_STATIC_WINDOW)
        self.gravity_fallback = False     # True when pitch/roll came purely from accel

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

    def _accel_window_is_static(self) -> bool:
        """True when the accelerometer window shows near-zero variance (the
        body is resting flat and gravity is a valid reference). Compares every
        window sample against the newest sample with an L∞ component check,
        relative to the mean |a| so it is scale-invariant."""
        w = self._accel_window
        if len(w) < ACCEL_STATIC_WINDOW:
            return False
        mags = [sqrt(x * x + y * y + z * z) for (x, y, z) in w]
        mean_mag = sum(mags) / len(mags)
        if mean_mag < ACCEL_STATIC_MIN_NORM:
            return False          # zero/degenerate vector: no gravity reference
        tol = ACCEL_STATIC_REL_TOL * mean_mag
        lx, ly, lz = w[-1]
        return all(max(abs(x - lx), abs(y - ly), abs(z - lz)) <= tol
                   for (x, y, z) in w)

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

        # ── Frozen-packet glitch detector (override #1) ──────────────────
        # Track the incoming gyro triple: if it stays EXACTLY identical (down
        # to the last decimal) for FROZEN_GYRO_PACKETS consecutive packets,
        # the I2C/FIFO buffer has locked up and the values are a stale dump,
        # not a measurement. A real parked MEMS gyro always shows micro-noise;
        # a real rotation never repeats bit-exactly at this packet cadence.
        triple = (gx, gy, gz)
        if triple == self._prev_gyro:
            self._gyro_repeat_run += 1
        else:
            self._gyro_repeat_run = 0
        self._prev_gyro = triple
        self.frozen_streak = self._gyro_repeat_run + 1
        frozen_glitch = (self._gyro_repeat_run >= FROZEN_GYRO_PACKETS - 1
                         and max(abs(gx), abs(gy), abs(gz)) > FROZEN_GLITCH_MIN_RATE)
        self.frozen_glitch = frozen_glitch
        if frozen_glitch:
            # Override the incoming gyro to exactly 0.0 BEFORE anything else
            # (warmup averaging, bias estimation, pose integration) consumes
            # it — this instantly freezes the angular-integration loop.
            gx = gy = gz = 0.0

        # ── Accelerometer static window (gravity-fallback gate) ─────────
        # Variance is measured over a short window in RAW firmware units and
        # compared RELATIVE to the mean magnitude, so the gate is invariant
        # to the sensor's unit scale (1 g hardware vs the mis-scaled 29.9
        # "g" bench unit behave identically).
        self._accel_window.append((ax, ay, az))
        accel_static = self._accel_window_is_static()

        # ── Accel rest-baseline learning (scale-invariant motion gate) ──
        # The rest magnitude+direction is learned ONLY while the gyro also
        # reports stillness — gravity is then the only specific force. This
        # prevents a sustained handling shake (constant in lab feeds) from
        # being mis-learned as the rest vector.
        a_norm = sqrt(ax * ax + ay * ay + az * az)
        band = (ACCEL_REST_BAND_FRAC * self._accel_rest_mag
                if self._accel_rest_mag is not None else None)
        # A frozen dump proves nothing about the body, so it must not be used
        # to learn the rest baseline either — the zeroed gyro would otherwise
        # look "still" and a garbage accel could be mis-learned as gravity,
        # permanently poisoning the motion gate.
        gyro_still_for_baseline = (self._gyro_still(gx, gy, gz)
                                   and not frozen_glitch)
        if (a_norm > 1e-6 and gyro_still_for_baseline
                and (band is None
                     or abs(a_norm - self._accel_rest_mag) <= band)):
            self._accel_still_run += 1
        else:
            self._accel_still_run = 0
        if (self._accel_still_run >= ACCEL_BASELINE_PACKETS
                and (band is None
                     or abs(a_norm - self._accel_rest_mag) <= band)):
            self._accel_rest_mag = a_norm
            inv = 1.0 / a_norm
            self._accel_rest_axis = (ax * inv, ay * inv, az * inv)
        # accel "looks like gravity": close to the learned rest magnitude
        # AND the direction is stable vs the learned rest axis (dot > 0.9).
        if self._accel_rest_mag is not None:
            band = ACCEL_REST_BAND_FRAC * self._accel_rest_mag
            mag_ok = abs(a_norm - self._accel_rest_mag) <= band
            inv = 1.0 / a_norm if a_norm > 1e-6 else 0.0
            dot = (ax * inv * self._accel_rest_axis[0]
                   + ay * inv * self._accel_rest_axis[1]
                   + az * inv * self._accel_rest_axis[2]) if inv else 0.0
            dir_ok = dot > 0.9
            accel_invalid = not (mag_ok and dir_ok)
        else:
            # Fallback until the baseline exists: the old absolute 1 g rule
            # (sane for correctly scaled sensors; harmless otherwise).
            accel_invalid = abs(a_norm - 1.0) > ACCEL_NORM_TOL

        # ── Bias warmup: average the first still samples to seed the bias.
        # Until calibrated (or the attempt cap gives up), track accel only so
        # the bias never leaks into the pose.
        if not self.bias_ready and self._warmup_attempts < BIAS_WARMUP_MAX_ATTEMPTS:
            # A frozen dump is not a calibration sample: skip accumulation
            # (the pose below is still anchored to the accel reference).
            if not frozen_glitch:
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
        if frozen_glitch:
            pass  # stale dump: never absorb it into the bias estimate
        elif not self._gyro_still(*raw):
            self._last_motion_ts = self._motion_time
        elif (self._motion_time - (self._last_motion_ts or 0.0)
              >= BIAS_HOLDOUT_SECONDS):
            # Parked: the raw reading IS the current bias — track it slowly.
            a = self._bias_ema_alpha(dt)
            self._bias = [b + a * (r - b)
                          for b, r in zip(self._bias, raw)]

        # ── Stale-gyro (frozen-sample) tracking (diagnostics only) ──
        # NOT a pose gate by itself anymore: defense-in-depth is layered —
        # 1) the frozen-packet detector above zeroes exactly-repeating large
        #    dumps before they reach the pose,
        # 2) the gravity fallback re-anchors pitch/roll to the accelerometer
        #    whenever gyro activity contradicts a static accel window,
        # 3) the complementary blend plus the parked bias EMA clean up any
        #    residual. This counter remains for ops diagnostics only.
        self._last_raw_gyro = raw

        # Remove estimated bias, then apply the small-signal dead-band.
        gx = apply_gyro_deadband(raw[0] - self._bias[0])
        gy = apply_gyro_deadband(raw[1] - self._bias[1])
        gz = apply_gyro_deadband(raw[2] - self._bias[2])
        if frozen_glitch:
            # Re-zero AFTER bias removal too: a stale bias estimate must not
            # leak a phantom residual (−bias) back into the pose.
            gx = gy = gz = 0.0

        gyro_active = max(abs(gx), abs(gy), abs(gz)) > MOTION_GYRO_THRESHOLD

        # ── Frozen-buffer veto (gyro-vs-accel contradiction) ─────────────
        # A REAL pitch/roll rotation swings the gravity vector through the
        # accelerometer within one packet gap — accel immediately leaves the
        # rest baseline. A frozen I2C buffer (observed: constant (-1.09,
        # 0.05, -0.96) rad/s for 90 s while parked => phantom -62 deg/s)
        # does NOT move accel. So "gx or gy claims rotation while accel
        # still reads rest" is impossible physics: the whole gyro sample is
        # stale — veto ALL axes for that packet (a frozen buffer corrupts
        # gz too).
        #
        # Per-axis nuance: a REAL yaw spin about the gravity axis leaves
        # accel untouched by definition, and a genuinely rotating body
        # shows near-zero gx/gy. Therefore: gz-only activity with a valid
        # accel is treated as a real yaw rotation and integrated; gx/gy
        # activity with a valid accel is declared frozen.
        gyro_active_gxgy = max(abs(gx), abs(gy)) > MOTION_GYRO_THRESHOLD
        buffer_frozen = gyro_active_gxgy and not accel_invalid
        self._contradiction_run = self._contradiction_run + 1 if buffer_frozen else 0

        # ── Gravity fallback (override #2) ───────────────────────────────
        # Gyro claims rotation while the accelerometer window is static
        # (near-zero variance = body resting flat): impossible physics.
        # Cut the gyro's fusion weight to 0 — pitch/roll come PURELY from the
        # accelerometer gravity vectors, which also re-anchors any pose that
        # a pre-detection runaway already corrupted. Yaw still integrates the
        # cleaned gz: a genuine spin about the gravity axis leaves accel
        # static BY DEFINITION, and gz is the only yaw reference there is
        # (a glitched packet's gz is already 0, so yaw holds there).
        gravity_fallback = accel_static and (frozen_glitch or gyro_active)
        self.gravity_fallback = gravity_fallback

        if gravity_fallback:
            self.pitch = _wrap180(pitch_acc)
            self.roll  = _wrap180(roll_acc)
        elif buffer_frozen:
            # Stale sample: hold the pose (blend to the trusted accel — a
            # no-op while accel is static — and never integrate yaw).
            self._blend_to_accel(pitch_acc, roll_acc, 0.0, 0.0, dt)
        elif accel_invalid or gyro_active:
            # Coast on gyro integration while the accel reference is
            # implausible (|a| off the rest baseline or direction changed)
            # or the body is rotating. Yaw always integrates: accel cannot
            # observe rotation about the gravity axis.
            self.pitch = _wrap180(self.pitch + gx * dt)
            self.roll  = _wrap180(self.roll  + gy * dt)
        else:
            self._blend_to_accel(pitch_acc, roll_acc, gx, gy, dt)
        if not buffer_frozen:
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
