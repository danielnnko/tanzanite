import json
import re
import base64
import os

_prev_seq = None
_lost_packets = 0

def get_stats() -> dict:
    global _lost_packets
    return {"lost_packets": _lost_packets}

def reset_stats():
    global _prev_seq, _lost_packets
    _prev_seq = None
    _lost_packets = 0

def frame_uplink(command: str) -> bytes:
    """Returns command + NMEA-style checksum + CRLF as bytes."""
    checksum = 0
    for char in command:
        checksum ^= ord(char)
    return f"{command}*{checksum:02X}\r\n".encode('ascii')

def _normalize_orb(res: dict) -> dict:
    """Normalize an ORB (propagator) packet so downstream code — history buffer,
    GIS map, tracking page — can treat it like a positioned TLM packet.

    Maps the ORB wire fields onto the names the physics pipeline produces:
      altitude_km  → true_altitude_m
      range_km     → slant_km
      range_rate   → range_rate_km_s
    and derives gps_lock from the position (non-null-island fix). ORB position
    comes from a propagator feed, not the onboard GPS receiver, so a valid fix
    there does not require the onboard GPS to have locked.
    """
    try:
        lat = float(res.get('latitude', 0.0) or 0.0)
        lon = float(res.get('longitude', 0.0) or 0.0)
        alt_km = float(res.get('altitude_km', 0.0) or 0.0)
        res['true_altitude_m'] = alt_km * 1000.0
        if res.get('range_km') is not None:
            res['slant_km'] = float(res['range_km'])
        if res.get('range_rate') is not None:
            res['range_rate_km_s'] = float(res['range_rate'])
        res['gps_lock'] = (lat != 0.0 and lon != 0.0)
    except (TypeError, ValueError):
        pass
    return res

def parse_line(line: str, image_buffer: dict, data_dir: str = 'data') -> dict | None:
    global _prev_seq, _lost_packets
    
    line = line.rstrip('\r\n\t ')
    if not line:
        return None

    rssi = None
    snr = None

    # Handle tags: " RSSI:-82dBm SNR:9.1dB" or " [RSSI=-82 SNR=9.1]"
    m_bracket = re.search(r'\[RSSI=([-\d.]+)\s+SNR=([-\d.]+)\]\s*$', line)
    m_space = re.search(r'RSSI:([-\d.]+)dBm\s+SNR:([-\d.]+)dB\s*$', line)
    
    if m_bracket:
        rssi = float(m_bracket.group(1))
        snr = float(m_bracket.group(2))
        line = line[:m_bracket.start()].rstrip()
    elif m_space:
        rssi = float(m_space.group(1))
        snr = float(m_space.group(2))
        line = line[:m_space.start()].rstrip()

    if not line:
        return None

    # Try JSON
    if line.startswith('{'):
        try:
            data = json.loads(line)
            data_lower = {k.lower(): v for k, v in data.items()}
            
            # Aliases
            if 'range' in data_lower and 'range_km' not in data_lower:
                data_lower['range_km'] = data_lower['range']
            if 'b64' in data_lower and 'image_b64' not in data_lower:
                data_lower['image_b64'] = data_lower['b64']
            if 'sat' in data_lower and 'satellite_id' not in data_lower:
                data_lower['satellite_id'] = data_lower['sat']
            if 'id' in data_lower and 'satellite_id' not in data_lower:
                data_lower['satellite_id'] = data_lower['id']
            if 'lat' in data_lower and 'latitude' not in data_lower:
                data_lower['latitude'] = data_lower['lat']
            if 'lon' in data_lower and 'longitude' not in data_lower:
                data_lower['longitude'] = data_lower['lon']
                
            res = data_lower
            if 'type' not in res and 'type' in data:
                res['type'] = data['type'] # Preserve original type if needed, but normally it's lowercase anyway
            if str(res.get('type', '')).upper() == 'ORB':
                res = _normalize_orb(res)
            if rssi is not None:
                res['rssi'] = rssi
                res['snr'] = snr
            return res
        except json.JSONDecodeError:
            pass

    # ACK routing
    ack_prefixes = ('ACK', 'OK', 'ERR', 'CMD_ACK', 'TX_OK', 'UPLINK_ACK')
    if any(line.startswith(p) for p in ack_prefixes) or ('ACK' in line and len(line) < 120):
        res = {'type': 'ack', 'message': line}
        if rssi is not None:
            res['rssi'] = rssi
            res['snr'] = snr
        return res

    parts = line.split(',')

    # Check tagged CSV
    tag = parts[0].upper()
    if tag in ('HK', 'BEACON'):
        if len(parts) >= 5:
            res = {
                'type': tag,
                'satellite_id': parts[1],
                'timestamp': parts[2],
                'battery_voltage': float(parts[3]),
                'internal_temperature': float(parts[4])
            }
            if rssi is not None:
                res['rssi'] = rssi
                res['snr'] = snr
            return res
    elif tag == 'ORB':
        if len(parts) >= 13:
            res = {
                'type': 'ORB',
                'next_aos': parts[1],
                'azimuth_at_aos': float(parts[2]),
                'next_los': parts[3],
                'max_elevation': float(parts[4]),
                'latitude': float(parts[5]),
                'longitude': float(parts[6]),
                'altitude_km': float(parts[7]),
                'range_km': float(parts[8]),
                'range_rate': float(parts[9]),
                'doppler_100mhz': float(parts[10]),
                'azimuth': float(parts[11]),
                'elevation': float(parts[12])
            }
            res = _normalize_orb(res)
            if rssi is not None:
                res['rssi'] = rssi
                res['snr'] = snr
            return res
    elif tag == 'IMG':
        if len(parts) >= 5:
            sat_id = parts[1]
            chunk_seq = int(parts[2])
            total_chunks = int(parts[3])
            b64_data = parts[4]
            
            buf_key = (sat_id, total_chunks)
            if buf_key not in image_buffer:
                image_buffer[buf_key] = {}

            # Proactive memory cap: abandon immediately if this chunk pushes
            # the buffered base64 size past the 2MB decoded-size budget,
            # rather than only checking after every chunk has already been
            # accumulated. A corrupt `total_chunks` field must not be able
            # to grow this buffer unbounded while we wait for it to fill.
            buffered_b64_len = sum(len(v) for v in image_buffer[buf_key].values()) + len(b64_data)
            if buffered_b64_len * 0.75 > 2_000_000:
                del image_buffer[buf_key]
                res = {'type': 'IMG', 'satellite_id': sat_id, 'chunk_seq': chunk_seq,
                       'total_chunks': total_chunks, 'error': 'image too large, abandoned'}
                if rssi is not None:
                    res['rssi'] = rssi
                    res['snr'] = snr
                return res

            image_buffer[buf_key][chunk_seq] = b64_data
            
            if len(image_buffer[buf_key]) == total_chunks:
                # Reassemble
                saved_filename = None
                try:
                    all_b64 = "".join(image_buffer[buf_key][i] for i in range(1, total_chunks + 1))
                    if len(all_b64) * 0.75 <= 2_000_000:
                        img_bytes = base64.b64decode(all_b64)
                        os.makedirs(os.path.join(data_dir, 'images'), exist_ok=True)
                        # We don't have a real acquisition timestamp in the IMG CSV
                        # fields, so we stamp the file with wall-clock receive time.
                        import time
                        ts = int(time.time())
                        saved_filename = f"{sat_id}_{ts}.jpg"
                        filepath = os.path.join(data_dir, 'images', saved_filename)
                        with open(filepath, 'wb') as f:
                            f.write(img_bytes)
                except Exception:
                    saved_filename = None
                del image_buffer[buf_key]

                res = {'type': 'image_ready' if saved_filename else 'IMG',
                       'satellite_id': sat_id, 'chunk_seq': chunk_seq, 'total_chunks': total_chunks}
                if saved_filename:
                    res['filename'] = saved_filename
                if rssi is not None:
                    res['rssi'] = rssi
                    res['snr'] = snr
                return res

            res = {'type': 'IMG', 'satellite_id': sat_id, 'chunk_seq': chunk_seq, 'total_chunks': total_chunks}
            if rssi is not None:
                res['rssi'] = rssi
                res['snr'] = snr
            return res
            
    # Live TLM (14 fields)
    if len(parts) == 14:
        try:
            temperature = float(parts[0])
            pressure_pa = float(parts[1])
            altitude_m = float(parts[2])
            accel_x = float(parts[3])
            accel_y = float(parts[4])
            accel_z = float(parts[5])
            gyro_x = float(parts[6])
            gyro_y = float(parts[7])
            gyro_z = float(parts[8])
            latitude = float(parts[9])
            longitude = float(parts[10])
            humidity = float(parts[11])
            bus_voltage = float(parts[12])
            seq = int(parts[13])

            # NaN/Inf guard: float() happily accepts firmware 'nan'/'inf' text,
            # and stdlib json.dumps then serializes it as bare NaN — which is
            # INVALID JSON. One such packet makes /api/history unparseable in
            # every browser (response.json() throws), silently killing the map
            # view ("AWAITING SATELLITE POSITION" forever). Reject the packet.
            values = (temperature, pressure_pa, altitude_m, accel_x, accel_y,
                      accel_z, gyro_x, gyro_y, gyro_z, latitude, longitude,
                      humidity, bus_voltage)
            if any(v != v or v in (float('inf'), float('-inf')) for v in values):
                return None

            gps_lock = True
            if latitude == 0.0 or longitude == 0.0:
                gps_lock = False
                
            if _prev_seq is not None:
                gap = seq - _prev_seq - 1
                if 0 < gap < 10000:
                    _lost_packets += gap
            _prev_seq = seq

            res = {
                'type': 'tlm',
                'temperature': temperature,
                'pressure_pa': pressure_pa,
                'altitude_m': altitude_m,
                'accel_x': accel_x,
                'accel_y': accel_y,
                'accel_z': accel_z,
                'gyro_x': gyro_x,
                'gyro_y': gyro_y,
                'gyro_z': gyro_z,
                'latitude': latitude,
                'longitude': longitude,
                'humidity': humidity,
                'bus_voltage': bus_voltage,
                'seq': seq,
                'gps_lock': gps_lock
            }
            if not gps_lock:
                res['azimuth'] = 0.0
                res['elevation'] = 0.0
                res['range'] = 0.0
                res['slant'] = 0.0
                res['horizontal'] = 0.0

            if rssi is not None:
                res['rssi'] = rssi
                res['snr'] = snr

            return res

        except ValueError:
            pass
            
    return None
