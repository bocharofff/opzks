# -*- coding: utf-8 -*-
"""
tests/test_cli_exports.py — src.cli.do_exports: выбор сети для heatmap по SSID
(без MAC), с обработкой коллизии одинаковых имён у разных физических точек.
"""

import csv
import os

import pytest

from src import cli
from src.db import init_db, insert_observation, upsert_network

NET_A = {
    "bssid": "AA:BB:CC:DD:EE:01", "ssid": "NetA", "encryption": "WPA2-PSK",
    "manufacturer": "Alfa", "channel": 6, "frequency": 2437.0,
}
NET_A_DUP = {
    "bssid": "FF:EE:DD:CC:BB:AA", "ssid": "NetA", "encryption": "Open",
    "manufacturer": "Other", "channel": 1, "frequency": 2412.0,
}
NET_B = {
    "bssid": "AA:BB:CC:DD:EE:02", "ssid": "NetB", "encryption": "Open",
    "manufacturer": "TP-Link", "channel": 11, "frequency": 2462.0,
}


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "wifi_monitor.db")
    conn = init_db(path)
    upsert_network(conn, NET_A)
    upsert_network(conn, NET_B)
    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": "2026-06-02T12:00:00Z",
        "lat": 47.2225, "lon": 39.7188, "rssi": -50, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_B["bssid"], "timestamp": "2026-06-02T12:01:00Z",
        "lat": 47.3, "lon": 39.8, "rssi": -60, "channel": 11, "frequency": 2462.0,
    })
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    """Перенаправляет do_exports в изолированный каталог (не в реальный export/)."""
    out = str(tmp_path / "export_out")
    monkeypatch.setattr(cli, "EXPORT_DIR", out)
    return out


def _heatmap_csv_path(export_dir):
    files = [f for f in os.listdir(export_dir) if f.startswith("heatmap_") and f.endswith(".csv")]
    assert len(files) == 1
    return os.path.join(export_dir, files[0])


def test_export_by_ssid_unique_resolves_to_bssid(db_path, export_dir):
    cli.do_exports(db_path, ["heatmap"], bssid_filter=None, ssid_filter="NetA")

    out_path = _heatmap_csv_path(export_dir)
    rows = _read_csv(out_path)
    assert len(rows) - 1 == 1
    assert rows[1][3] == NET_A["bssid"]  # отфильтровано именно по NetA


def test_export_by_ssid_unknown_name_skips_heatmap(db_path, export_dir, caplog):
    cli.do_exports(db_path, ["heatmap"], bssid_filter=None, ssid_filter="НетТакойСети")

    # Профиль пропущен — файла heatmap_*.csv быть не должно
    files = [f for f in os.listdir(export_dir) if f.startswith("heatmap_")]
    assert files == []
    assert "не найдена" in caplog.text


def test_export_by_ssid_collision_skips_heatmap_but_not_others(db_path, export_dir, caplog):
    # Добавляем вторую физическую точку с ТЕМ ЖЕ именем — коллизия
    conn = init_db(db_path)
    upsert_network(conn, NET_A_DUP)
    conn.commit()
    conn.close()

    cli.do_exports(db_path, ["heatmap", "ap_status"], bssid_filter=None, ssid_filter="NetA")

    # heatmap пропущен (нужна MAC для разрешения коллизии)
    heatmap_files = [f for f in os.listdir(export_dir) if f.startswith("heatmap_")]
    assert heatmap_files == []
    assert "совпадает с 2 разными точками" in caplog.text
    assert NET_A["bssid"] in caplog.text
    assert NET_A_DUP["bssid"] in caplog.text

    # ap_status при этом отработал независимо
    ap_status_files = [f for f in os.listdir(export_dir) if f.startswith("ap_status_")]
    assert len(ap_status_files) == 1


def test_explicit_bssid_takes_priority_over_ssid(db_path, export_dir):
    # Если почему-то заданы оба — явный --export-bssid не переопределяется SSID
    cli.do_exports(
        db_path, ["heatmap"], bssid_filter=NET_B["bssid"], ssid_filter="NetA",
    )
    out_path = _heatmap_csv_path(export_dir)
    rows = _read_csv(out_path)
    assert rows[1][3] == NET_B["bssid"]
