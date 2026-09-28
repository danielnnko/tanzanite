import pytest
from tansat.parser import parse_line, get_stats, reset_stats, frame_uplink

def setup_function():
    reset_stats()

def test_rssi_snr_tag_stripping():
    # space-separated
    res1 = parse_line("ACK testing RSSI:-82dBm SNR:9.1dB", {})
    assert res1 is not None
    assert res1['rssi'] == -82.0
    assert res1['snr'] == 9.1
    assert res1['message'] == 'ACK testing'

    # bracketed
    res2 = parse_line("ACK testing [RSSI=-82 SNR=9.1]", {})
    assert res2 is not None
    assert res2['rssi'] == -82.0
    assert res2['snr'] == 9.1
    assert res2['message'] == 'ACK testing'

def test_bare_tlm():
    line = "25.0,101325,100,0,0,1.0,0.1,0.2,0.3,34.0,-118.0,50.0,4.1,5"
    res = parse_line(line, {})
    assert res is not None
    assert res['type'] == 'tlm'
    assert res['temperature'] == 25.0
    assert res['gps_lock'] is True
    assert res['latitude'] == 34.0
    assert res['seq'] == 5

def test_gps_fix_rule():
    line = "25.0,101325,100,0,0,1.0,0.1,0.2,0.3,0.0,-118.0,50.0,4.1,5"
    res = parse_line(line, {})
    assert res is not None
    assert res['gps_lock'] is False
    assert res['azimuth'] == 0.0
    assert res['elevation'] == 0.0
    
def test_wrong_field_count():
    line = "25.0,101325,100,0,0,1.0,0.1,0.2,0.3,34.0,-118.0,50.0,4.1" # 13 fields
    res = parse_line(line, {})
    assert res is None

def test_json_hk():
    line = '{"type":"HK","satellite_id":"sat1","battery_voltage":4.1}'
    res = parse_line(line, {})
    assert res is not None
    assert res['type'] == 'HK'
    assert res['satellite_id'] == 'sat1'

def test_tagged_csv():
    # HK
    hk_line = "HK,sat1,2024-01-01T00:00:00,3.85,23.1"
    res = parse_line(hk_line, {})
    assert res['type'] == 'HK'
    assert res['satellite_id'] == 'sat1'
    assert res['battery_voltage'] == 3.85
    
    # BEACON
    bc_line = "BEACON,sat1,2024-01-01T00:00:00,3.85,23.1"
    res2 = parse_line(bc_line, {})
    assert res2['type'] == 'BEACON'

def test_ack_line_routing():
    res = parse_line("TX_OK: payload sent", {})
    assert res is not None
    assert res['type'] == 'ack'
    assert res['message'] == 'TX_OK: payload sent'

def test_frame_uplink():
    out = frame_uplink('PING')
    assert out == b'PING*10\r\n'

def test_packet_loss():
    parse_line("25.0,101325,100,0,0,1.0,0.1,0.2,0.3,34.0,-118.0,50.0,4.1,5", {})
    parse_line("25.0,101325,100,0,0,1.0,0.1,0.2,0.3,34.0,-118.0,50.0,4.1,8", {})
    stats = get_stats()
    assert stats['lost_packets'] == 2

def test_image_chunk_reassembly(tmp_path):
    import base64
    img_data = b"fake_jpeg_data"
    b64_data = base64.b64encode(img_data).decode('utf-8')
    part1 = b64_data[:5]
    part2 = b64_data[5:]
    
    buf = {}
    res1 = parse_line(f"IMG,sat1,1,2,{part1}", buf, data_dir=str(tmp_path))
    assert res1['type'] == 'IMG'
    assert ('sat1', 2) in buf
    
    res2 = parse_line(f"IMG,sat1,2,2,{part2}", buf, data_dir=str(tmp_path))
    assert res2['type'] == 'image_ready'
    assert res2['filename'].startswith('sat1_')
    assert ('sat1', 2) not in buf
    
    # check file
    images_dir = tmp_path / 'images'
    assert images_dir.exists()
    files = list(images_dir.glob('sat1_*.jpg'))
    assert len(files) == 1
    with open(files[0], 'rb') as f:
        assert f.read() == img_data

