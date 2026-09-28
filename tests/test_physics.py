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
