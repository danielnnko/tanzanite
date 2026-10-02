#!/usr/bin/env python3
"""
tools/replay_attitude.py — Gyro-Runaway verification replay.

Feeds UNCLEANED ground-station data through tansat.physics.AttitudeFilter and
prints the corrected Pitch / Roll / Yaw tracking values, so the frozen-gyro
glitch fix can be verified on recorded sessions or on a synthetic glitch.

Usage:
  python tools/replay_attitude.py data/session_20261002T081930Z.csv
  python tools/replay_attitude.py data/session_*.csv --limit 50
  python tools/replay_attitude.py --demo            # synthetic MPU6050 lockup
  python tools/replay_attitude.py --demo --legacy   # pre-fix behaviour, for A/B

CSV input columns used (raw firmware values, exactly as the modem emitted
them — this script does NOT pre-clean anything):
  accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z  (+ optional server_time)
"""

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tansat import physics  # noqa: E402

def demo_stream():
    """Synthetic reproduction of the reported fault:
    calm bench rest → fast rotation → MPU6050 I2C/FIFO lockup with the gyro
    frozen at (0, 0.97, -1.06) rad/s while accel returns to a flat rest."""
    FLAT = (0.0, 0.33, 0.0)               # "flat, resting" accel after lockup
    FROZEN = (0.0, 0.97, -1.06)           # the reported frozen dump (rad/s)
    stream = []
    for _ in range(8):                    # A: calm, parked (bias/baseline warmup)
        stream.append(((0.0, 0.0, 1.0, 0.0, 0.0, 0.0), 3.0, "A calm"))
    for i in range(4):                    # B: fast rotation, gyro values VARY
        gx = 0.95 + 0.02 * i              # rad/s — a real spin is never bit-identical
        stream.append(((0.9, -0.2, 0.9, gx, 0.02, -0.9), 3.0, "B spin"))
    for _ in range(12):                   # C: lockup — frozen dump + flat accel
        stream.append((FLAT + FROZEN, 3.0, "C lockup"))
    return stream

def _fmt_gyro(gx, gy, gz):
    return f"({gx:7.3f},{gy:7.3f},{gz:7.3f})"


def _row_imu(row):
    """Extract the raw IMU sample from one CSV row. None if malformed."""
    try:
        return tuple(float((row.get(k) or "").strip())
                     for k in ("accel_x", "accel_y", "accel_z",
                               "gyro_x", "gyro_y", "gyro_z"))
    except (TypeError, ValueError):
        return None


def _dt_seconds(row, prev_ts, default):
    """Packet interval from server_time deltas; default when unavailable."""
    raw = (row.get("server_time") or "").strip()
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return default, prev_ts
    if prev_ts is not None:
        dt = (ts - prev_ts).total_seconds()
        if 0.0 < dt <= physics.DT_MAX:
            return dt, ts
    return default, ts


def _print_header():
    print(f"{'#':>5}  {'gyro_x,gyro_y,gyro_z (raw)':<27} "
          f"{'FLAG':<12} {'pitch':>8} {'roll':>8} {'yaw':>8}  phase")


def _print_row(i, imu, filt, phase=""):
    flag = "FROZEN" if filt.frozen_glitch else \
           ("accel-fb" if filt.gravity_fallback else "")
    print(f"{i:>5}  {_fmt_gyro(*imu[3:])} "
          f"{flag:<12} {filt.pitch:8.2f} {filt.roll:8.2f} {filt.yaw:8.2f}  {phase}")


def replay(imu_stream, gyro_scale, alpha, dt_default, limit=0):
    """Run the stream through the filter; returns (filter, rows_printed)."""
    f = physics.AttitudeFilter(alpha=alpha, gyro_scale=gyro_scale)
    printed = 0
    for i, (imu, dt, phase) in enumerate(imu_stream, start=1):
        f.update(*imu, dt)
        if limit <= 0 or printed < limit:
            _print_row(i, imu, f, phase)
            printed += 1
    return f, printed


def iter_csv(paths, dt_default):
    """Yield (imu, dt, phase) tuples from session CSVs in file order."""
    for path in paths:
        prev_ts = None
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                imu = _row_imu(row)
                if imu is None:
                    continue
                dt, prev_ts = _dt_seconds(row, prev_ts, dt_default)
                yield imu, dt, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="*", help="session CSV file(s) to replay")
    ap.add_argument("--demo", action="store_true",
                    help="run the synthetic frozen-gyro demo instead of CSVs")
    ap.add_argument("--legacy", action="store_true",
                    help="disable the new detectors to show the pre-fix runaway")
    ap.add_argument("--gyro-scale", type=float, default=None,
                    help="firmware gyro unit → °/s multiplier "
                         "(default: 57.2958 rad/s hardware for --demo, "
                         "tansat GYRO_SCALE for CSV replay)")
    ap.add_argument("--alpha", type=float, default=physics.ATTITUDE_ALPHA,
                    help="complementary-filter accel weight (default 0.98)")
    ap.add_argument("--dt", type=float, default=3.0,
                    help="fallback packet interval when server_time is unusable")
    ap.add_argument("--limit", type=int, default=200,
                    help="max rows to print (0 = all)")
    args = ap.parse_args()

    if args.gyro_scale is not None:
        gyro_scale = args.gyro_scale
    else:
        gyro_scale = 57.2958              # this hardware reports rad/s in every stream

    if args.legacy:
        physics.FROZEN_GYRO_PACKETS = 10 ** 9        # detector off
        physics.ACCEL_STATIC_MIN_NORM = float("inf")  # fallback off
        print(">> LEGACY MODE: frozen-gyro detector and gravity fallback disabled\n")

    if args.demo:
        source = demo_stream()
        title = "SYNTHETIC DEMO — reported lockup: gyro frozen (0, 0.97, -1.06) rad/s"
    elif args.csv:
        source = iter_csv(args.csv, args.dt)
        title = f"REPLAY — {', '.join(args.csv)}"
    else:
        ap.error("pass one or more session CSVs, or --demo")

    print(title)
    print(f"   gyro_scale={gyro_scale}  alpha={args.alpha}  "
          f"frozen_detector={'OFF' if args.legacy else physics.FROZEN_GYRO_PACKETS} packets\n")
    _print_header()
    filt, printed = replay(source, gyro_scale, args.alpha, args.dt, args.limit)

    print(f"\nprinted rows: {printed}" +
          ("  (use --limit 0 for all)" if printed == args.limit else ""))
    print(f"final corrected pose:  pitch={filt.pitch:.2f}°  "
          f"roll={filt.roll:.2f}°  yaw={filt.yaw:.2f}°")
    if filt.frozen_glitch:
        print("stream ended with the gyro still FROZEN (integration suppressed).")


if __name__ == "__main__":
    main()
