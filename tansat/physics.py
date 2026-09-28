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

# §3.4 Complementary attitude filter — alpha and gyro scale are runtime-tunable
# via the admin settings page (keys: attitude_alpha, gyro_scale).
ATTITUDE_ALPHA = 0.98     # accel weight per packet (lower = more responsive)
GYRO_SCALE   = 1.0        # firmware gyro units → °/s multiplier
DT_MIN, DT_MAX = 0.005, 0.5

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

    def update(self, ax, ay, az, gx, gy, gz, dt):
        gx = apply_gyro_deadband(gx * self.gyro_scale)
        gy = apply_gyro_deadband(gy * self.gyro_scale)
        gz = apply_gyro_deadband(gz * self.gyro_scale)
        dt = max(DT_MIN, min(DT_MAX, dt))
        pitch_acc, roll_acc = accel_orientation(ax, ay, az)
        if self._first:
            self.pitch = pitch_acc
            self.roll  = roll_acc
            self._first = False
            return self.pitch, self.roll, self.yaw
        self.pitch = self.alpha * (self.pitch + gx * dt) + (1 - self.alpha) * pitch_acc
        self.roll  = self.alpha * (self.roll  + gy * dt) + (1 - self.alpha) * roll_acc
        self.yaw   = (self.yaw + gz * dt) % 360.0
        if self.yaw > 180:
            self.yaw -= 360.0
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
