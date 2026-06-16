"""
tests/test_db.py — тесты для src/db.py
"""

import time
import pytest
from src.db import (
    init_db,
    upsert_network,
    insert_observation,
    insert_ap_health,
    checkpoint,
)

# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

NETWORK = {
    "bssid": "AA:BB:CC:DD:EE:FF",
    "ssid": "TestNet",
    "encryption": "WPA2",
    "manufacturer": "TestCorp",
    "channel": 6,
    "frequency": 2437.0,
}

OBSERVATION = {
    "bssid": "AA:BB:CC:DD:EE:FF",
    "timestamp": "2026-06-02T12:00:00Z",
    "lat": 55.75,
    "lon": 37.62,
    "rssi": -65,
    "channel": 6,
    "frequency": 2437.0,
}

AP_HEALTH = {
    "ap_id": "ap-garage-01",
    "timestamp": "2026-06-02T12:00:00Z",
    "status": "ok",
    "rtt_ms": 42.5,
    "lat": 55.75,
    "lon": 37.62,
}


# ---------------------------------------------------------------------------
# Структура БД
# ---------------------------------------------------------------------------

def test_tables_created(conn):
    """Все три таблицы должны существовать после init_db."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    tables = {row[0] for row in rows}
    assert "networks" in tables
    assert "observations" in tables
    assert "ap_health" in tables


def test_wal_mode_enabled(conn):
    """WAL mode должен быть включён."""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


# ---------------------------------------------------------------------------
# upsert_network
# ---------------------------------------------------------------------------

def test_upsert_network_insert(conn):
    """Первая вставка: запись появляется, first_seen и last_seen заполнены."""
    upsert_network(conn, NETWORK)

    row = conn.execute(
        "SELECT * FROM networks WHERE bssid = ?", (NETWORK["bssid"],)
    ).fetchone()

    assert row is not None
    assert row["ssid"] == "TestNet"
    assert row["encryption"] == "WPA2"
    assert row["first_seen"] is not None
    assert row["last_seen"] is not None


def test_upsert_network_update(conn):
    """
    Повторный upsert с тем же bssid:
      - first_seen не меняется
      - last_seen обновляется
      - изменённое поле (ssid) перезаписывается
    """
    upsert_network(conn, NETWORK)
    first = conn.execute(
        "SELECT first_seen, last_seen FROM networks WHERE bssid = ?",
        (NETWORK["bssid"],),
    ).fetchone()

    # Небольшая пауза, чтобы last_seen гарантированно отличился
    time.sleep(1.1)

    updated = {**NETWORK, "ssid": "UpdatedNet"}
    upsert_network(conn, updated)

    second = conn.execute(
        "SELECT ssid, first_seen, last_seen FROM networks WHERE bssid = ?",
        (NETWORK["bssid"],),
    ).fetchone()

    assert second["ssid"] == "UpdatedNet"
    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] > first["last_seen"]

# ---------------------------------------------------------------------------
# insert_observation
# ---------------------------------------------------------------------------

def test_insert_observation_with_gps(conn):
    """Наблюдение с координатами: has_gps=1, lat/lon сохранены."""
    upsert_network(conn, NETWORK)
    rowid = insert_observation(conn, OBSERVATION)

    row = conn.execute(
        "SELECT * FROM observations WHERE id = ?", (rowid,)
    ).fetchone()

    assert row is not None
    assert row["has_gps"] == 1
    assert abs(row["lat"] - 55.75) < 1e-6
    assert abs(row["lon"] - 37.62) < 1e-6
    assert row["rssi"] == -65


def test_insert_observation_without_gps(conn):
    """Наблюдение без координат: has_gps=0, lat=None, lon=None."""
    upsert_network(conn, NETWORK)
    obs_no_gps = {
        "bssid": NETWORK["bssid"],
        "timestamp": "2026-06-02T12:01:00Z",
        "rssi": -70,
    }
    rowid = insert_observation(conn, obs_no_gps)

    row = conn.execute(
        "SELECT * FROM observations WHERE id = ?", (rowid,)
    ).fetchone()

    assert row["has_gps"] == 0
    assert row["lat"] is None
    assert row["lon"] is None


# ---------------------------------------------------------------------------
# insert_ap_health
# ---------------------------------------------------------------------------

def test_insert_ap_health_ok(conn):
    """Запись со статусом ok и rtt_ms сохраняется корректно."""
    rowid = insert_ap_health(conn, AP_HEALTH)

    row = conn.execute(
        "SELECT * FROM ap_health WHERE id = ?", (rowid,)
    ).fetchone()

    assert row is not None
    assert row["ap_id"] == "ap-garage-01"
    assert row["status"] == "ok"
    assert abs(row["rtt_ms"] - 42.5) < 1e-6
    assert row["has_gps"] == 1


def test_insert_ap_health_failed(conn):
    """Запись с status='no_inet' и rtt_ms=None: rtt_ms должен быть NULL."""
    data = {
        "ap_id": "ap-garage-01",
        "timestamp": "2026-06-02T12:05:00Z",
        "status": "no_inet",
        "rtt_ms": None,
    }
    rowid = insert_ap_health(conn, data)

    row = conn.execute(
        "SELECT rtt_ms, status FROM ap_health WHERE id = ?", (rowid,)
    ).fetchone()

    assert row["status"] == "no_inet"
    assert row["rtt_ms"] is None


# ---------------------------------------------------------------------------
# Накопление наблюдений
# ---------------------------------------------------------------------------

def test_multiple_observations_accumulate(conn):
    """Три наблюдения для одной сети накапливаются в таблице observations."""
    upsert_network(conn, NETWORK)

    for i in range(3):
        insert_observation(conn, {
            **OBSERVATION,
            "timestamp": f"2026-06-02T12:0{i}:00Z",
            "rssi": -60 - i,
        })

    rows = conn.execute(
        "SELECT * FROM observations WHERE bssid = ?", (NETWORK["bssid"],)
    ).fetchall()

    assert len(rows) == 3
    assert all(row["bssid"] == NETWORK["bssid"] for row in rows)


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------

def test_checkpoint_runs(conn):
    """checkpoint() не должен бросать исключений."""
    upsert_network(conn, NETWORK)
    checkpoint(conn)  # просто не упасть