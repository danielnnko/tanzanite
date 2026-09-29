import pytest
from tansat.physics import (
    calculate_azimuth_and_range,
    accel_orientation,
    apply_gyro_deadband,
    AttitudeFilter,
    GpsFilter,
    battery_percent
)

def test_azimuth_north():
    # same lon, dev is north
    az, horiz, slant, el = calculate_azimuth_and_range(0.0, 0.0, 0.0, 10.0, 0.0, 0.0)
    assert pytest.approx(az, 0.1) == 0.0

def test_azimuth_east():
    # same lat, dev is east
    az, horiz, slant, el = calculate_azimuth_and_range(0.0, 0.0, 0.0, 0.0, 10.0, 0.0)
    assert pytest.approx(az, 0.1) == 90.0

def test_elevation_above():
    az, horiz, slant, el = calculate_azimuth_and_range(0.0, 0.0, 0.0, 0.0, 0.0, 1000.0)
    assert pytest.approx(el, 0.1) == 90.0

def test_accel_orientation_flat():
    pitch, roll = accel_orientation(0.0, 0.0, 1.0)
    assert pytest.approx(pitch, 0.1) == 0.0
    assert pytest.approx(roll, 0.1) == 0.0

def test_gyro_deadband():
    assert apply_gyro_deadband(0.03) == 0.0
    assert apply_gyro_deadband(0.1) == 0.1
    assert apply_gyro_deadband(-0.04) == 0.0

def test_attitude_filter_seeding():
    f = AttitudeFilter()
    pitch, roll, yaw = f.update(0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    # y=1, z=1 => roll is atan2(1,1) = 45 deg
    assert pytest.approx(roll, 0.1) == 45.0
    assert pytest.approx(yaw, 0.1) == 0.0

def test_attitude_filter_gyro_responsiveness():
    # Rotating at a realistic hand-rotation rate must move the attitude
    # promptly once calibrated, not sit near zero.
    f = AttitudeFilter()
    for _ in range(10):                             # complete bias warmup
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.02)
    p, r, y = (None, None, None)
    for _ in range(25):                             # 25 packets * 0.02 s = 0.5 s
        p, r, y = f.update(0.0, 0.0, 1.0, 90.0, 0.0, 0.0, 0.02)
    # 90 deg/s for ~0.5 s => ~45 deg of pitch
    assert abs(p) > 30.0, f"attitude barely moved: pitch={p}"

def test_attitude_filter_motion_gate_blocks_noisy_accel():
    # While |a| >> 1 g (device being handled), garbage accel must NOT be
    # blended in; attitude should track the gyro instead.
    f = AttitudeFilter()
    f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.02)   # seed at rest, accel is pure gravity
    for _ in range(30):                             # violent shake, no rotation
        p, r, _ = f.update(4.0, 4.0, 1.0, 0.0, 0.0, 0.0, 0.02)
    # accel-derived roll from (4,4,1) is atan2(4, sqrt(17)) ≈ 44 deg —
    # that garbage must stay out of the estimate.
    assert abs(r) < 10.0, f"noisy accel leaked into attitude: roll={r}"

def test_attitude_filter_reanchors_after_motion_stops():
    # After motion stops and accel is a clean 1 g, the filter must re-anchor
    # to the accel reference instead of crawling.
    f = AttitudeFilter()
    f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.02)
    for _ in range(30):                             # handle it: accel noise, rotating
        f.update(3.0, -2.0, 1.0, 60.0, 40.0, 0.0, 0.02)
    # Now at rest in a clearly tilted pose: ay=az=1/sqrt(2) => roll ≈ 45 deg
    # and |a| = 1 g (a valid gravity vector).
    g = 0.5 ** 0.5
    p, r, _ = f.update(0.0, g, g, 0.0, 0.0, 0.0, 0.02)
    for _ in range(150):                            # ~3 s of blending
        p, r, _ = f.update(0.0, g, g, 0.0, 0.0, 0.0, 0.02)
    assert abs(r - 45.0) < 3.0, f"did not re-anchor to accel reference: roll={r}"

def test_gps_filter_no_fix():
    f = GpsFilter()
    lat, lon, valid = f.update(0.0, 0.0)
    assert valid is False

def test_gps_filter_deadband():
    f = GpsFilter()
    f.update(10.0, 10.0)
    # move tiny amount
    lat, lon, valid = f.update(10.0000001, 10.0000001)
    assert lat == 10.0
    assert lon == 10.0
    assert valid is True

def test_battery_percent():
    assert pytest.approx(battery_percent(3.30), 0.1) == 0.0
    assert pytest.approx(battery_percent(4.20), 0.1) == 100.0
    assert pytest.approx(battery_percent(3.80), 0.1) == 40.0
    
def test_battery_above_5v():
    assert battery_percent(85.0) == 85.0

# ── Gyro bias calibration ────────────────────────────────────────────

def test_bias_calibrated_from_warmup():
    # 10 still packets with a constant bias on gx: after warmup the filter
    # must subtract it (subsequent integration must not move).
    f = AttitudeFilter(gyro_scale=57.2958)          # rad/s firmware
    BIAS = 0.02                                     # rad/s on x
    for _ in range(20):                             # > warmup, all at rest
        p, r, y = f.update(0.0, 0.0, 1.0, BIAS, 0.0, 0.0, 2.0)
    assert abs(p) < 0.5 and abs(r) < 0.5 and abs(y) < 0.5, (p, r, y)

def test_long_gap_rotation_fully_integrated():
    # A 90 deg/s rotation sampled every 3 s (dt clamp old max was 0.5 s)
    # must integrate the FULL interval, not a truncated one.
    f = AttitudeFilter()
    for _ in range(12):                             # warmup at rest
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    p, r, y = 0.0, 0.0, 0.0
    for _ in range(2):                              # two packets, 3 s apart
        p, r, y = f.update(0.0, 0.0, 1.0, 90.0, 0.0, 0.0, 3.0)
    # ~6 s at 90 deg/s (minus a little for the moving-average lag) => >90 deg
    assert abs(p) > 90.0, f"rotation truncated: pitch={p}"

def test_bias_holdout_after_motion():
    # Right after motion stops, the residual rotation tail must NOT be
    # absorbed into the bias (bias unchanged), so the holdout must keep the
    # gyro integrating (even if ~zero) rather than snapping the pose.
    f = AttitudeFilter()
    for _ in range(12):                             # warmup
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    f.update(0.0, 0.0, 1.0, 30.0, 0.0, 0.0, 0.1)    # a spin
    bias_before = list(f._bias)
    p, r, y = f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)  # first still packet
    assert f._bias == bias_before                   # untouched during holdout

def test_bias_ema_tracks_slow_drift():
    # After warmup at zero bias, a parked device with a small sub-threshold
    # residual (0.1 deg/s — below MOTION_GYRO_THRESHOLD, i.e. indistinguishable
    # from drift-of-bias rather than real motion) must pull the bias estimate
    # toward it via the EMA.
    f = AttitudeFilter()
    for _ in range(12):                             # warmup at true bias 0
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    for _ in range(500):                            # parked, residual 0.1 deg/s
        f.update(0.0, 0.0, 1.0, 0.1, 0.0, 0.0, 0.1)
    assert abs(f._bias[0] - 0.1) < 0.02, f"bias estimate stuck: {f._bias}"

def test_bias_not_updated_during_motion():
    # Strong rotation is indistinguishable from bias — the estimator must
    # not chase it.
    f = AttitudeFilter()
    for _ in range(12):                             # warmup
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    bias_before = list(f._bias)
    for _ in range(20):                             # hard spin
        f.update(0.0, 0.0, 1.0, 90.0, 0.0, 0.0, 0.1)
    assert abs(f._bias[0] - bias_before[0]) < 1e-9, f"bias chased motion: {f._bias}"

def test_rad_s_pipeline_end_to_end():
    # Full sanity: rad/s firmware (scale 57.2958), biased gyro, 3 s packets,
    # BROKEN accel (|a|=0.5 g — matches current real hardware). Rotate 90 deg/s
    # then park: the pose must hold with no bias drift (pure dead-reckoning).
    f = AttitudeFilter(gyro_scale=57.2958)
    bias = [0.02, 0.03, 0.01]                       # rad/s, as measured
    bad_accel = (0.0, -0.44, 0.0)                   # |a| = 0.44 g, always invalid
    for _ in range(12):                             # warmup at rest
        f.update(*bad_accel, *bias, 3.0)
    p, r, y = 0.0, 0.0, 0.0
    for _ in range(2):                              # 90 deg/s for ~6 s total
        p, r, y = f.update(*bad_accel, 90/57.2958 + bias[0], bias[1], bias[2], 3.0)
    # ~6 s at 90 deg/s => ~540 deg, wraps to 180 after _wrap180
    assert abs(p) > 170.0, f"spin under-integrated: pitch={p}"
    for _ in range(10):                             # park for ~30 s
        p2, r2, y2 = f.update(*bad_accel, *bias, 3.0)
    # No valid accel anchor -> pure gyro coast. Bias was removed at warmup,
    # so the pose must hold.
    def wrap_diff(a, b):
        return abs((a - b + 180.0) % 360.0 - 180.0)
    assert wrap_diff(p2, p) < 10.0, f"parked drift too high: {p} -> {p2}"

def test_bias_recovers_from_wrong_seed():
    # Regression: if warmup seeds a wrong bias (satellite moving at connect),
    # the parked device reads a constant raw rate (e.g. 1.72 deg/s from a
    # -0.03 rad/s bias). The estimator MUST track that raw signal while
    # parked — the old holdout locked itself out on the bias-corrected
    # residual and the pose drifted forever.
    f = AttitudeFilter(gyro_scale=57.2958)
    for _ in range(12):                             # warmup at rest (bias 0)
        f.update(0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1)
    p, r, y = 0.0, 0.0, 0.0
    for _ in range(40):                             # parked, raw -0.03 rad/s on gy
        p, r, y = f.update(0.0, 0.0, 1.0, 0.0, 1.72/57.2958, 0.0, 3.0)
    # EMA pulls the bias toward +1.72 deg/s; residual shrinks to ~0
    assert abs(f._bias[1] - 1.72) < 0.2, f"bias not tracking: {f._bias}"
    # and the pose must stop integrating the phantom rate (drift << naive 40*3*1.72)
    assert abs(r) < 15.0, f"pose ran away: roll={r}"

def test_rad_s_reanchors_with_valid_accel():
    # Same spin, but with a healthy flat accelerometer: after parking, the
    # accel anchor must pull the pose back to the true (flat) orientation.
    f = AttitudeFilter(gyro_scale=57.2958)
    bias = [0.02, 0.03, 0.01]
    for _ in range(12):
        f.update(0.0, 0.0, 1.0, *bias, 3.0)
    for _ in range(2):
        f.update(0.0, 0.0, 1.0, 90/57.2958 + bias[0], bias[1], bias[2], 3.0)
    for _ in range(20):                             # park ~60 s, flat, at rest
        p2, r2, y2 = f.update(0.0, 0.0, 1.0, *bias, 3.0)
    assert abs(p2) < 5.0, f"did not re-anchor to flat: pitch={p2}"
