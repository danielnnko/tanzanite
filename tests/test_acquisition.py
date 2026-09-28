"""Unit tests for tansat/acquisition.py."""

import time
import pytest
from tansat.acquisition import ControlLock, ClientRegistry, HistoryBuffer, MAX_CLIENT_QUEUE


def test_control_lock_basic():
    lock = ControlLock(ttl=2)
    # Take lock
    ok, msg = lock.take("operator1")
    assert ok is True
    assert lock.is_held_by("operator1") is True
    assert lock.is_held_by("operator2") is False

    status = lock.status()
    assert status["held"] is True
    assert status["holder"] == "operator1"

    # Another operator cannot take lock
    ok2, msg2 = lock.take("operator2")
    assert ok2 is False

    # Heartbeat
    ok_hb, _ = lock.heartbeat("operator1")
    assert ok_hb is True
    ok_hb_wrong, _ = lock.heartbeat("operator2")
    assert ok_hb_wrong is False

    # Normal release by non-holder fails
    ok_rel, _ = lock.release("operator2")
    assert ok_rel is False

    # Release by holder succeeds
    ok_rel2, _ = lock.release("operator1")
    assert ok_rel2 is True
    assert lock.status()["held"] is False


def test_control_lock_force_seize():
    lock = ControlLock(ttl=60)
    lock.take("operator1")
    assert lock.is_held_by("operator1")

    # Admin force-seizes lock
    ok, msg = lock.take("admin1", force=True)
    assert ok is True
    assert lock.is_held_by("admin1")
    assert not lock.is_held_by("operator1")


def test_client_registry_fanout():
    registry = ClientRegistry()
    cid1, q1 = registry.register()
    cid2, q2 = registry.register()
    assert registry.client_count() == 2

    # Broadcast
    registry.broadcast("status", {"test": 123})

    msg1 = q1.get(timeout=1.0)
    msg2 = q2.get(timeout=1.0)
    assert "event: status" in msg1
    assert '"test": 123' in msg1
    assert msg1 == msg2

    # Unregister
    registry.unregister(cid1)
    assert registry.client_count() == 1
    registry.unregister(cid2)
    assert registry.client_count() == 0


def test_history_buffer():
    buf = HistoryBuffer(maxlen=5)
    for i in range(10):
        buf.append({"seq": i})

    snap = buf.snapshot()
    assert len(snap) == 5
    assert [p["seq"] for p in snap] == [5, 6, 7, 8, 9]

    limited = buf.snapshot(limit=2)
    assert len(limited) == 2
    assert [p["seq"] for p in limited] == [8, 9]
