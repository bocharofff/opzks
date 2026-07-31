# -*- coding: utf-8 -*-
"""
tests/test_kismet_runner.py — чтение packets/devices Kismet и синхронизация в нашу БД.

Собираем временную SQLite «как у Kismet» (таблицы devices + packets) и проверяем:
  - read_ap_metadata читает метаданные точек;
  - read_kismet_packets даёт по одному сэмплу на bucket_sec (сильнейший сигнал,
    его lat/lon), инкрементально по packetid, с фильтром по AP-сетям;
  - sync_kismet_to_db пишет наблюдения и разрешает координату:
    packets.lat/lon → gps.position_at(ts) → has_gps=0.
"""

import json
import sqlite3

import pytest

from src import kismet_runner
from src.db import init_db


AP_MAC = "AA:BB:CC:DD:EE:01"
OTHER_MAC = "11:22:33:44:55:66"  # не AP — должен отфильтроваться


def _device_blob(ssid="MyNet", crypt="WPA2-PSK", manuf="Realtek", channel="6", freq_khz=2437000):
    return json.dumps({
        "kismet.device.base.commonname": ssid,
        "kismet.device.base.crypt": crypt,
        "kismet.device.base.manuf": manuf,
        "kismet.device.base.channel": channel,
        "kismet.device.base.frequency": freq_khz,
    })


@pytest.fixture
def kismet_db(tmp_path):
    """Путь к синтетической базе Kismet с devices и packets."""
    path = str(tmp_path / "scan.kismet")
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE devices (
            devmac TEXT, type TEXT, device TEXT
        );
        CREATE TABLE packets (
            packetid  INTEGER,
            ts_sec    INTEGER,
            phyname   TEXT,
            sourcemac TEXT,
            signal    INTEGER,
            lat       REAL,
            lon       REAL,
            frequency REAL
        );
        """
    )
    db.execute(
        "INSERT INTO devices (devmac, type, device) VALUES (?, 'Wi-Fi AP', ?)",
        (AP_MAC, _device_blob()),
    )
    db.execute(
        "INSERT INTO devices (devmac, type, device) VALUES (?, 'Wi-Fi Client', ?)",
        (OTHER_MAC, _device_blob(ssid="client")),
    )

    # Секунда 100: два пакета AP — сильнейший -40 в точке (11,21)
    # Секунда 101: один пакет AP -55 в точке (12,22)
    rows = [
        (1, 100, "IEEE802.11", AP_MAC,   -50, 10.0, 20.0, 2437000),
        (2, 100, "IEEE802.11", AP_MAC,   -40, 11.0, 21.0, 2437000),  # max сигнал в бакете
        (3, 101, "IEEE802.11", AP_MAC,   -55, 12.0, 22.0, 2437000),
        # шум, который должен отфильтроваться:
        (4, 101, "IEEE802.11", OTHER_MAC, -30, 99.0, 99.0, 2437000),  # не AP
        (5, 101, "IEEE802.11", AP_MAC,     0, 13.0, 23.0, 2437000),   # signal=0
        (6, 101, "BLUETOOTH",  AP_MAC,   -20, 14.0, 24.0, 2437000),   # не Wi-Fi phy
    ]
    db.executemany(
        "INSERT INTO packets (packetid, ts_sec, phyname, sourcemac, signal, lat, lon, frequency)"
        " VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()
    db.close()
    return path


# ---------------------------------------------------------------------------
# read_ap_metadata
# ---------------------------------------------------------------------------

def test_read_ap_metadata(kismet_db):
    meta = kismet_runner.read_ap_metadata(kismet_db)
    assert set(meta.keys()) == {AP_MAC}       # только type='Wi-Fi AP'
    m = meta[AP_MAC]
    assert m["ssid"] == "MyNet"
    assert m["encryption"] == "WPA2-PSK"
    assert m["channel"] == 6
    assert m["frequency"] == pytest.approx(2437.0)  # кГц → МГц


# ---------------------------------------------------------------------------
# read_kismet_packets — даунсэмплинг, фильтр, инкрементальность
# ---------------------------------------------------------------------------

def test_read_packets_downsample_and_filter(kismet_db):
    samples, last_pid = kismet_runner.read_kismet_packets(kismet_db, since_packetid=0, bucket_sec=1)

    # По одному сэмплу на секунду для AP: секунды 100 и 101
    assert len(samples) == 2
    assert last_pid == 6                        # max packetid в таблице

    by_ts = {s["ts_sec"]: s for s in samples}
    # В секунде 100 сильнейший сигнал -40, координата из ЕГО строки (11,21)
    assert by_ts[100]["signal"] == -40
    assert by_ts[100]["lat"] == 11.0
    assert by_ts[100]["lon"] == 21.0
    # Секунда 101 — только валидный AP-пакет -55 (signal=0 и BT отфильтрованы)
    assert by_ts[101]["signal"] == -55
    # Чужой MAC (99,99) не просочился
    assert all(s["lat"] != 99.0 for s in samples)


def test_read_packets_incremental(kismet_db):
    # Пропускаем всё до packetid=2 → остаётся только секунда 101
    samples, last_pid = kismet_runner.read_kismet_packets(kismet_db, since_packetid=2, bucket_sec=1)
    assert last_pid == 6
    assert len(samples) == 1
    assert samples[0]["ts_sec"] == 101


def test_read_packets_no_new(kismet_db):
    samples, last_pid = kismet_runner.read_kismet_packets(kismet_db, since_packetid=6, bucket_sec=1)
    assert samples == []
    assert last_pid == 6


def test_read_packets_bucket_two_seconds(kismet_db):
    # bucket_sec=2 → секунды 100 и 101 попадают в один бакет → 1 сэмпл (сильнейший -40)
    samples, _ = kismet_runner.read_kismet_packets(kismet_db, since_packetid=0, bucket_sec=2)
    assert len(samples) == 1
    assert samples[0]["signal"] == -40


# ---------------------------------------------------------------------------
# sync_kismet_to_db — наблюдения и разрешение координаты
# ---------------------------------------------------------------------------

class _FakeGPS:
    """GPS-заглушка: position_at возвращает фикс только для ts=200."""
    def position_at(self, ts, tol=2.0):
        return {"lat": 5.5, "lon": 6.6} if ts == 200 else None

    def latest(self):
        return None


def test_sync_writes_observations_with_packet_coords(tmp_path, kismet_db):
    conn = init_db(str(tmp_path / "our.db"))
    try:
        count, last_pid = kismet_runner.sync_kismet_to_db(
            kismet_db, conn, gps=None, since_packetid=0, bucket_sec=1
        )
        assert count == 2
        assert last_pid == 6

        # Сеть в networks
        net = conn.execute("SELECT ssid, encryption FROM networks WHERE bssid=?", (AP_MAC,)).fetchone()
        assert net["ssid"] == "MyNet"

        # Наблюдения с координатами из packets и has_gps=1
        obs = conn.execute(
            "SELECT lat, lon, rssi, has_gps FROM observations ORDER BY timestamp"
        ).fetchall()
        assert len(obs) == 2
        assert all(o["has_gps"] == 1 for o in obs)
        assert {round(o["lat"], 1) for o in obs} == {11.0, 12.0}
    finally:
        conn.close()


def test_sync_uses_gps_position_at_when_packet_has_no_fix(tmp_path):
    # База, где у AP-пакета координаты 0/0 (нет фикса у Kismet), ts=200
    path = str(tmp_path / "nofix.kismet")
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE devices (devmac TEXT, type TEXT, device TEXT);"
        "CREATE TABLE packets (packetid INTEGER, ts_sec INTEGER, phyname TEXT,"
        " sourcemac TEXT, signal INTEGER, lat REAL, lon REAL, frequency REAL);"
    )
    db.execute("INSERT INTO devices VALUES (?, 'Wi-Fi AP', ?)", (AP_MAC, _device_blob()))
    db.execute(
        "INSERT INTO packets VALUES (?,?,?,?,?,?,?,?)",
        (1, 200, "IEEE802.11", AP_MAC, -60, 0.0, 0.0, 2437000),
    )
    db.commit()
    db.close()

    conn = init_db(str(tmp_path / "our2.db"))
    try:
        count, _ = kismet_runner.sync_kismet_to_db(
            path, conn, gps=_FakeGPS(), since_packetid=0, bucket_sec=1
        )
        assert count == 1
        obs = conn.execute("SELECT lat, lon, has_gps FROM observations").fetchone()
        # Координата подставлена из НАШЕГО трека по времени пакета
        assert obs["lat"] == 5.5
        assert obs["lon"] == 6.6
        assert obs["has_gps"] == 1
    finally:
        conn.close()


def test_sync_no_gps_marks_observation_without_coords(tmp_path):
    path = str(tmp_path / "nofix2.kismet")
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE devices (devmac TEXT, type TEXT, device TEXT);"
        "CREATE TABLE packets (packetid INTEGER, ts_sec INTEGER, phyname TEXT,"
        " sourcemac TEXT, signal INTEGER, lat REAL, lon REAL, frequency REAL);"
    )
    db.execute("INSERT INTO devices VALUES (?, 'Wi-Fi AP', ?)", (AP_MAC, _device_blob()))
    db.execute(
        "INSERT INTO packets VALUES (?,?,?,?,?,?,?,?)",
        (1, 300, "IEEE802.11", AP_MAC, -60, 0.0, 0.0, 2437000),
    )
    db.commit()
    db.close()

    conn = init_db(str(tmp_path / "our3.db"))
    try:
        count, _ = kismet_runner.sync_kismet_to_db(path, conn, gps=None, since_packetid=0)
        assert count == 1
        obs = conn.execute("SELECT lat, lon, has_gps FROM observations").fetchone()
        assert obs["lat"] is None and obs["lon"] is None
        assert obs["has_gps"] == 0     # запись сохранена, но помечена как без координат
    finally:
        conn.close()
