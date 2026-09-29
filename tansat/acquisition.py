"""
tansat/acquisition.py
─────────────────────
Single-process acquisition service.

Responsibilities:
  • Read the serial (or TCP) modem stream in one thread.
  • Parse every line via tansat.parser, run physics via tansat.physics.
  • Fan-out processed packets to every SSE client via per-client bounded queues.
  • Maintain a rolling history buffer so a late-joining browser can back-fill charts.
  • Arbitrate the control lock (one holder at a time; 90-s TTL; admin force-seize).
  • Write CSV session logs to disk.

No web-framework import here. The Flask routes import this module and call its API.
"""

import csv
import io
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports

from tansat import parser as tlm_parser
from tansat import physics

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────
MAX_CLIENT_QUEUE   = 200   # drop oldest when full (never stall the reader)
HISTORY_BUFFER_LEN = 500   # packets kept for late-joiner back-fill
CONTROL_LOCK_TTL   = 90    # seconds; refreshed by heartbeat
CSV_FLUSH_INTERVAL = 5     # flush CSV writer every N seconds


# ──────────────────────────────────────────────────────────
# Control lock
# ──────────────────────────────────────────────────────────
class ControlLock:
    """
    Single control lock.  At most one operator may hold it at a time.
    TTL enforced: if the holder's heartbeat stops, the lock expires automatically.
    Admin can force-seize regardless of current holder.
    """

    def __init__(self, ttl: int = CONTROL_LOCK_TTL):
        self._lock     = threading.Lock()
        self._holder: Optional[str] = None   # username
        self._expires: float        = 0.0
        self._ttl      = ttl

    # ── Public API ──

    def take(self, username: str, force: bool = False) -> tuple[bool, str]:
        """
        Returns (success, message).
        force=True is only for admin force-seize (caller must check role before passing).
        """
        with self._lock:
            now = time.monotonic()
            if self._holder and self._expires > now and not force:
                remaining = int(self._expires - now)
                return False, f"Control held by '{self._holder}' ({remaining}s remaining)"
            self._holder  = username
            self._expires = now + self._ttl
            return True, f"Control acquired by '{username}'"

    def release(self, username: str, force: bool = False) -> tuple[bool, str]:
        """Release. force=True allows admin to release another user's lock."""
        with self._lock:
            if self._holder is None:
                return True, "Lock was already free"
            if self._holder != username and not force:
                return False, f"Lock held by '{self._holder}', not '{username}'"
            prev = self._holder
            self._holder  = None
            self._expires = 0.0
            return True, f"Control released (was '{prev}')"

    def heartbeat(self, username: str) -> tuple[bool, str]:
        """Refresh TTL. Fails silently if user no longer holds the lock."""
        with self._lock:
            now = time.monotonic()
            if self._holder == username and self._expires > now:
                self._expires = now + self._ttl
                return True, "heartbeat OK"
            return False, "heartbeat rejected (lock not held)"

    def is_held_by(self, username: str) -> bool:
        with self._lock:
            return (self._holder == username
                    and self._expires > time.monotonic())

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._holder and self._expires > now:
                return {
                    "held": True,
                    "holder": self._holder,
                    "expires_in": round(self._expires - now, 1),
                }
            return {"held": False, "holder": None, "expires_in": 0}


# ──────────────────────────────────────────────────────────
# SSE client registry
# ──────────────────────────────────────────────────────────
class ClientRegistry:
    """Thread-safe registry of connected SSE clients."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._clients: dict[int, queue.Queue] = {}   # id → queue
        self._next_id = 0

    def register(self) -> tuple[int, queue.Queue]:
        q = queue.Queue(maxsize=MAX_CLIENT_QUEUE)
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            self._clients[cid] = q
        return cid, q

    def unregister(self, cid: int) -> None:
        with self._lock:
            self._clients.pop(cid, None)

    def broadcast(self, event: str, data: dict) -> None:
        """Fan-out to all clients; drop oldest on full queue (never block)."""
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            clients = list(self._clients.values())
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Drop oldest message, then put new one
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


# ──────────────────────────────────────────────────────────
# Rolling history buffer
# ──────────────────────────────────────────────────────────
class HistoryBuffer:
    """Thread-safe circular buffer of the last N processed packets."""

    def __init__(self, maxlen: int = HISTORY_BUFFER_LEN):
        self._lock  = threading.Lock()
        self._buf: list[dict] = []
        self._maxlen = maxlen

    def append(self, packet: dict) -> None:
        with self._lock:
            if len(self._buf) >= self._maxlen:
                self._buf.pop(0)
            self._buf.append(packet)

    def snapshot(self, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            buf = list(self._buf)
        return buf[-limit:] if limit else buf


# ──────────────────────────────────────────────────────────
# Session-log replay (cold-start back-fill)
# ──────────────────────────────────────────────────────────
_TLM_FLOATS = (
    "temperature", "pressure_pa", "altitude_m",
    "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
    "latitude", "longitude", "humidity", "bus_voltage",
    "azimuth", "elevation", "slant_km", "range_rate_km_s",
    "doppler_100mhz", "battery_pct", "pitch", "roll", "yaw",
)


def _csv_row_to_packet(row: dict, gs_alt: float = 0.0) -> Optional[dict]:
    """Convert one SessionLogger CSV row back into a TLM packet dict.

    The CSV stores only the raw+derived scalars; fields the frontend expects
    (type, gps_lock bool, true_altitude_m) are reconstructed here.
    Returns None for malformed rows.
    """
    try:
        pkt: dict = {"type": "tlm"}
        for key in _TLM_FLOATS:
            raw = (row.get(key) or "").strip()
            pkt[key] = float(raw) if raw else 0.0
            # Skip rows with non-finite values: float('nan') would serialize
            # as bare NaN in /api/history JSON — invalid for every browser.
            if pkt[key] != pkt[key] or pkt[key] in (float("inf"), float("-inf")):
                return None
        pkt["seq"] = int((row.get("seq") or "0").strip() or 0)
        pkt["gps_lock"] = (row.get("gps_lock") or "").strip().lower() == "true"
        for key in ("rssi", "snr"):
            raw = (row.get(key) or "").strip()
            pkt[key] = float(raw) if raw else None
        pkt["server_ts"] = (row.get("server_time") or "").strip() or None
        pkt["true_altitude_m"] = gs_alt + pkt["altitude_m"]
        return pkt
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────
# CSV session logger
# ──────────────────────────────────────────────────────────
class SessionLogger:
    """Writes processed TLM packets to a CSV session file."""

    FIELDS = [
        "server_time", "seq", "temperature", "pressure_pa", "altitude_m",
        "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
        "latitude", "longitude", "humidity", "bus_voltage",
        "gps_lock", "azimuth", "elevation", "slant_km",
        "range_rate_km_s", "doppler_100mhz", "battery_pct",
        "pitch", "roll", "yaw", "rssi", "snr",
    ]

    def __init__(self, data_dir: str = "data"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._path   = self._data_dir / f"session_{ts}.csv"
        self._file   = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS,
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._last_flush = time.monotonic()
        logger.info("Session log: %s", self._path)

    def write(self, row: dict) -> None:
        # Packets carry "server_ts" (ISO string); the CSV column is named
        # "server_time" — alias it here so session logs stay timestamped and
        # the history back-fill can restore timing after a restart.
        out = dict(row)
        if not out.get("server_time"):
            out["server_time"] = row.get("server_ts") or ""
        self._writer.writerow(out)
        if time.monotonic() - self._last_flush >= CSV_FLUSH_INTERVAL:
            self._file.flush()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()
        logger.info("Session log closed: %s", self._path)

    @property
    def filename(self) -> str:
        return self._path.name


# ──────────────────────────────────────────────────────────
# Acquisition service (the singleton)
# ──────────────────────────────────────────────────────────
class AcquisitionService:
    """
    The one-and-only acquisition service.  Created once at app startup.
    Thread-safe; designed to run in a single process.
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir    = data_dir
        self.control_lock = ControlLock()
        self.clients      = ClientRegistry()
        self.history      = HistoryBuffer()

        # State
        self._serial: Optional[serial.Serial] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock    = threading.Lock()

        # Physics state (per-connection lifecycle)
        self._attitude_filter  = physics.AttitudeFilter()
        self._gps_filter       = physics.GpsFilter()
        self._range_rate_filter = physics.RangeRateFilter()
        self._accel_warned = False   # accel-health watchdog latch (per connection)

        # Session logger
        self._session_log: Optional[SessionLogger] = None

        # Image reassembly buffer
        self._image_buffer: dict = {}

        # Ground-station config (updated by admin settings)
        self._gs_lat  = -6.813737
        self._gs_lon  = 39.280427
        self._gs_alt  = 30.0    # metres MSL
        self._alt_offset = 0.0  # calibration offset
        # Attitude filter tuning (updated by admin settings)
        self._attitude_alpha = physics.ATTITUDE_ALPHA
        self._gyro_scale   = physics.GYRO_SCALE

        # Cold-start back-fill: the rolling history is in-memory only, so
        # after every app restart the map/earth views had nothing to show
        # until fresh packets arrived. Replay the newest session CSVs.
        self._backfill_history_from_disk()

    def _backfill_history_from_disk(self, max_files: int = 3) -> None:
        """Seed the history buffer from the newest session CSVs on disk."""
        data_dir = Path(self.data_dir)
        if not data_dir.exists():
            return
        files = sorted(data_dir.glob("session_*.csv"), reverse=True)[:max_files]
        seeded: list[dict] = []
        for path in files:
            try:
                with open(path, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        pkt = _csv_row_to_packet(row, gs_alt=self._gs_alt)
                        if pkt is not None:
                            seeded.append(pkt)
            except OSError as e:
                logger.warning("History back-fill could not read %s: %s", path, e)
            if len(seeded) >= HISTORY_BUFFER_LEN:
                break
        for pkt in seeded[-HISTORY_BUFFER_LEN:]:
            self.history.append(pkt)
        if seeded:
            logger.info("History back-filled with %d packets from session logs",
                        min(len(seeded), HISTORY_BUFFER_LEN))

    # ── Configuration ──

    def update_gs_config(self, gs_lat: float, gs_lon: float,
                          gs_alt: float, alt_offset: float = 0.0) -> None:
        with self._lock:
            self._gs_lat     = gs_lat
            self._gs_lon     = gs_lon
            self._gs_alt     = gs_alt
            self._alt_offset = alt_offset

    def update_attitude_config(self, alpha: float, gyro_scale: float) -> None:
        """Update attitude-filter tuning live (no restart needed).

        Applied immediately to the running AttitudeFilter so a settings save
        takes effect on the very next telemetry packet.
        """
        with self._lock:
            self._attitude_alpha = alpha
            self._gyro_scale   = gyro_scale
            f = self._attitude_filter
            if f is not None:
                f.alpha = max(0.0, min(1.0, float(alpha)))
                f.gyro_scale = float(gyro_scale)

    # ── Control ──

    def start(self, port: str, baud: int = 9600) -> tuple[bool, str]:
        with self._lock:
            if self._running:
                return False, "Already running"
            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=baud,
                    timeout=1.0,
                )
            except serial.SerialException as e:
                return False, f"Serial open failed: {e}"

            # Reset physics state on new acquisition
            self._attitude_filter  = physics.AttitudeFilter(
                alpha=self._attitude_alpha,
                gyro_scale=self._gyro_scale,
            )
            self._gps_filter       = physics.GpsFilter()
            self._range_rate_filter = physics.RangeRateFilter()
            self._accel_warned = False
            tlm_parser.reset_stats()
            self._image_buffer = {}

            # Open session log
            self._session_log = SessionLogger(self.data_dir)

            self._running = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True, name="tansat-reader"
            )
            self._reader_thread.start()

            msg = f"Acquisition started on {port} @ {baud} baud"
            logger.info(msg)
            self.clients.broadcast("status", self._build_status())
            return True, msg

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self._running:
                return False, "Not running"
            self._running = False
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
            if self._session_log:
                self._session_log.close()
                self._session_log = None

        if self._reader_thread:
            self._reader_thread.join(timeout=3.0)
            self._reader_thread = None

        msg = "Acquisition stopped"
        logger.info(msg)
        self.clients.broadcast("status", self._build_status())
        return True, msg

    # ── Status ──

    def is_running(self) -> bool:
        return self._running

    def _build_status(self) -> dict:
        stats = tlm_parser.get_stats()
        return {
            "acquisition_running": self._running,
            "port": self._serial.port if self._serial else None,
            "baud": self._serial.baudrate if self._serial else None,
            "clients_connected": self.clients.client_count(),
            "control_lock": self.control_lock.status(),
            "lost_packets": stats.get("lost_packets", 0),
            "session_log": (self._session_log.filename
                            if self._session_log else None),
            "gs_lat": self._gs_lat,
            "gs_lon": self._gs_lon,
            "gs_alt": self._gs_alt,
        }

    def get_status(self) -> dict:
        return self._build_status()

    def get_history(self, limit: Optional[int] = None) -> list[dict]:
        return self.history.snapshot(limit)

    # ── Uplink ──

    def send_uplink(self, command: str) -> tuple[bool, str]:
        """Send a framed uplink command over the open serial port."""
        with self._lock:
            if not self._running or not self._serial:
                return False, "Not connected"
            try:
                framed = tlm_parser.frame_uplink(command)
                self._serial.write(framed)
                self.clients.broadcast("uplink", {
                    "direction": "tx",
                    "raw": framed.decode("ascii", errors="replace").strip(),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                return True, "Sent"
            except serial.SerialException as e:
                return False, f"Serial write error: {e}"

    # ── Reader loop ──

    def _reader_loop(self) -> None:
        logger.info("Reader thread started")
        prev_packet_time: Optional[float] = None

        while self._running:
            ser = self._serial
            if ser is None or not ser.is_open:
                break
            try:
                raw_bytes = ser.readline()
            except serial.SerialException as e:
                logger.error("Serial read error: %s", e)
                self.clients.broadcast("error", {"message": str(e)})
                break

            if not raw_bytes:
                continue

            raw_line = raw_bytes.decode("ascii", errors="replace")

            # Broadcast raw line for the downlink console
            self.clients.broadcast("raw", {
                "line": raw_line.rstrip(),
                "ts": datetime.now(timezone.utc).isoformat(),
            })

            # Parse
            try:
                packet = tlm_parser.parse_line(
                    raw_line, self._image_buffer, self.data_dir
                )
            except Exception as e:
                logger.warning("Parser exception: %s | line: %r", e, raw_line)
                packet = None

            if packet is None:
                continue

            ptype = packet.get("type")

            # Route ACK lines to the uplink channel, not telemetry
            if ptype == "ack":
                self.clients.broadcast("uplink", {
                    "direction": "rx",
                    "raw": packet.get("message", ""),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                continue

            # Route image-ready events
            if ptype == "image_ready":
                self.clients.broadcast("image", packet)
                continue

            # ORB propagator packets carry their own position/geometry (already
            # normalized by the parser), so they must NOT go through the TLM
            # physics pipeline — but they are kept in history so a late-joining
            # browser can back-fill the satellite's last known position.
            if ptype == "ORB":
                self.history.append(packet)

            # Only process TLM packets through physics
            if ptype == "tlm":
                now = time.monotonic()
                dt  = (now - prev_packet_time) if prev_packet_time is not None else 0.02
                prev_packet_time = now

                packet = self._apply_physics(packet, dt, now)
                self.history.append(packet)

                if self._session_log:
                    self._session_log.write(packet)

            self.clients.broadcast("packet", packet)

        logger.info("Reader thread exiting")
        # If loop exits unexpectedly, clean up
        with self._lock:
            if self._running:
                self._running = False
                if self._session_log:
                    self._session_log.close()
                    self._session_log = None
        self.clients.broadcast("status", self._build_status())

    # ── Physics pipeline ──

    def _apply_physics(self, pkt: dict, dt: float, now: float) -> dict:
        """Apply all physics transforms to a raw TLM packet dict."""

        # 1. GPS jitter filter
        raw_lat = pkt.get("latitude", 0.0) or 0.0
        raw_lon = pkt.get("longitude", 0.0) or 0.0
        f_lat, f_lon, gps_lock = self._gps_filter.update(raw_lat, raw_lon)
        pkt["gps_lock"] = gps_lock
        pkt["latitude"]  = f_lat if gps_lock else 0.0
        pkt["longitude"] = f_lon if gps_lock else 0.0

        # 2. True MSL altitude
        raw_alt_m  = pkt.get("altitude_m", 0.0) or 0.0
        true_alt_m = self._gs_alt + (raw_alt_m - self._alt_offset)
        pkt["true_altitude_m"] = true_alt_m

        # 3. Pointing geometry
        if gps_lock and f_lat is not None and f_lon is not None:
            az, horiz, slant, elev = physics.calculate_azimuth_and_range(
                self._gs_lat, self._gs_lon, self._gs_alt,
                f_lat, f_lon, true_alt_m
            )
            pkt["azimuth"]   = round(az,    2)
            pkt["elevation"] = round(elev,  2)
            pkt["slant_m"]   = round(slant, 1)
            pkt["slant_km"]  = round(slant / 1000.0, 3)
            pkt["horizontal_m"] = round(horiz, 1)
        else:
            pkt["azimuth"] = pkt["elevation"] = pkt["slant_m"] = 0.0
            pkt["slant_km"] = pkt["horizontal_m"] = 0.0

        # 4. Range-rate + Doppler (derived; overridden by ORB packet if present)
        rr, dop = self._range_rate_filter.update(
            pkt.get("slant_m", 0.0), now, gps_lock
        )
        pkt.setdefault("range_rate_km_s", rr)
        pkt.setdefault("doppler_100mhz",  dop)

        # 5. Attitude filter
        ax = pkt.get("accel_x", 0.0) or 0.0
        ay = pkt.get("accel_y", 0.0) or 0.0
        az_val = pkt.get("accel_z", 0.0) or 0.0
        gx = pkt.get("gyro_x",  0.0) or 0.0
        gy = pkt.get("gyro_y",  0.0) or 0.0
        gz = pkt.get("gyro_z",  0.0) or 0.0
        pitch, roll, yaw = self._attitude_filter.update(
            ax, ay, az_val, gx, gy, gz, dt
        )
        pkt["pitch"] = round(pitch, 2)
        pkt["roll"]  = round(roll,  2)
        pkt["yaw"]   = round(yaw,   2)

        # 5b. Accel-health watchdog: real gravity always reads ~1 g. A resting
        # device far outside that band (dead axis, wrong sensitivity, wrong
        # units) invalidates the gravity anchor — surface it loudly once per
        # connection so the firmware issue isn't mistaken for filter tuning.
        a_norm = (ax * ax + ay * ay + az_val * az_val) ** 0.5
        if a_norm > 1.35 or a_norm < 0.65:
            if not self._accel_warned:
                self._accel_warned = True
                logger.warning(
                    "Accelerometer health check failed: |a|=%.2f g at rest "
                    "(expected ~1 g; ax=%.2f ay=%.2f az=%.2f). Pitch/roll "
                    "have NO gravity anchor — check firmware accel scaling, "
                    "units (g vs m/s^2) and for dead axes.",
                    a_norm, ax, ay, az_val,
                )

        # 6. Battery percent
        voltage = pkt.get("bus_voltage", 0.0) or 0.0
        pkt["battery_pct"] = round(physics.battery_percent(voltage), 1)

        # 7. Server timestamp
        pkt["server_ts"] = datetime.now(timezone.utc).isoformat()

        return pkt


# ──────────────────────────────────────────────────────────
# Singleton instance (imported by Flask routes)
# ──────────────────────────────────────────────────────────
_service: Optional[AcquisitionService] = None


def init_service(data_dir: str = "data") -> AcquisitionService:
    global _service
    if _service is None:
        _service = AcquisitionService(data_dir=data_dir)
    return _service


def get_service() -> AcquisitionService:
    if _service is None:
        raise RuntimeError("Acquisition service not initialised. Call init_service() first.")
    return _service


# ── Convenience re-export for auth.py ──
control_lock: ControlLock = None   # set to _service.control_lock after init


def _resolve_control_lock():
    """Called by init_service; makes tansat.acquisition.control_lock accessible."""
    global control_lock
    if _service:
        control_lock = _service.control_lock


# ── Serial port enumeration ──
def list_serial_ports() -> list[dict]:
    """Return available serial ports visible to the server process."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device":      p.device,
            "description": p.description or "",
            "hwid":        p.hwid or "",
        })
    return ports
