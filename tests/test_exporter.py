# -*- coding: utf-8 -*-
"""
tests/test_exporter.py — форматы экспорта раздела 13 ТЗ.

Профили: heatmap (CSV), heatmap_networks (CSV на сеть + манифест),
full (GeoPackage + CSV, оба файла всегда), wigle (WigleWifi-1.4), ap_status (CSV).
"""

import csv
import os
import re
import sqlite3
import struct

import pytest

from src import exporter
from src.db import insert_ap_health, insert_observation, upsert_network

NET_A = {
    "bssid": "AA:BB:CC:DD:EE:01", "ssid": "NetA", "encryption": "WPA2-PSK",
    "manufacturer": "Alfa", "channel": 6, "frequency": 2437.0,
}
NET_B = {
    "bssid": "AA:BB:CC:DD:EE:02", "ssid": "NetB Open", "encryption": "Open",
    "manufacturer": "TP-Link", "channel": 11, "frequency": 2462.0,
}


def _seed(conn):
    """Две сети: NetA — 2 GPS-наблюдения; NetB — 1 GPS + 1 без координат."""
    upsert_network(conn, NET_A)
    upsert_network(conn, NET_B)

    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": "2026-06-02T12:00:00Z",
        "lat": 47.2225, "lon": 39.7188, "rssi": -50, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": "2026-06-02T12:00:05Z",
        "lat": 47.2226, "lon": 39.7189, "rssi": -55, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_B["bssid"], "timestamp": "2026-06-02T12:01:00Z",
        "lat": 47.3, "lon": 39.8, "rssi": -60, "channel": 11, "frequency": 2462.0,
    })
    # Наблюдение без GPS: должно попасть в full CSV, но не в heatmap/GPKG
    insert_observation(conn, {
        "bssid": NET_B["bssid"], "timestamp": "2026-06-02T12:02:00Z",
        "rssi": -70, "channel": 11, "frequency": 2462.0,
    })
    insert_ap_health(conn, {
        "ap_id": "ap-garage-01", "timestamp": "2026-06-02T12:00:00Z",
        "status": "ok", "rtt_ms": 23.5, "lat": 47.2225, "lon": 39.7188,
    })
    conn.commit()


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


NET_C = {
    "bssid": "AA:BB:CC:DD:EE:03", "ssid": "NetC", "encryption": "WPA2-PSK",
    "manufacturer": "Alfa", "channel": 1, "frequency": 2412.0,
}

DAY1 = "2026-07-20"
DAY2 = "2026-07-23"


def _seed_multi_day(conn):
    """Данные за два разных дня — для тестов --since/--until.

    День 1 (2026-07-20): NetA (2 набл.), NetC (1 набл., ТОЛЬКО в этот день).
    День 2 (2026-07-23): NetA (1 набл., др. координаты), NetB (1 набл.).
    ap_health: по одной записи на каждый день.
    """
    upsert_network(conn, NET_A)
    upsert_network(conn, NET_B)
    upsert_network(conn, NET_C)

    # День 1
    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": DAY1 + "T10:00:00Z",
        "lat": 10.0, "lon": 20.0, "rssi": -50, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": DAY1 + "T10:00:05Z",
        "lat": 10.1, "lon": 20.1, "rssi": -55, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_C["bssid"], "timestamp": DAY1 + "T11:00:00Z",
        "lat": 11.0, "lon": 21.0, "rssi": -40, "channel": 1, "frequency": 2412.0,
    })
    insert_ap_health(conn, {
        "ap_id": "ap-1", "timestamp": DAY1 + "T10:00:00Z",
        "status": "ok", "rtt_ms": 10.0, "lat": 10.0, "lon": 20.0,
    })

    # День 2
    insert_observation(conn, {
        "bssid": NET_A["bssid"], "timestamp": DAY2 + "T09:00:00Z",
        "lat": 30.0, "lon": 40.0, "rssi": -60, "channel": 6, "frequency": 2437.0,
    })
    insert_observation(conn, {
        "bssid": NET_B["bssid"], "timestamp": DAY2 + "T09:30:00Z",
        "lat": 31.0, "lon": 41.0, "rssi": -65, "channel": 11, "frequency": 2462.0,
    })
    insert_ap_health(conn, {
        "ap_id": "ap-1", "timestamp": DAY2 + "T09:00:00Z",
        "status": "no_inet", "rtt_ms": None, "lat": 30.0, "lon": 40.0,
    })

    conn.commit()


# ---------------------------------------------------------------------------
# heatmap (одиночный CSV)
# ---------------------------------------------------------------------------

def test_heatmap_csv_header_and_gps_only(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "heatmap.csv")
    count = exporter.export_heatmap_csv(conn, out)

    rows = _read_csv(out)
    assert rows[0] == ["lat", "lon", "rssi", "bssid", "ssid", "timestamp"]
    assert count == 3            # 2 (NetA) + 1 (NetB c GPS); без-GPS исключено
    assert len(rows) - 1 == count


def test_heatmap_csv_bssid_filter(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "heatmap_a.csv")
    count = exporter.export_heatmap_csv(conn, out, bssid_filter=NET_A["bssid"])

    assert count == 2
    rows = _read_csv(out)
    assert all(r[3] == NET_A["bssid"] for r in rows[1:])


# ---------------------------------------------------------------------------
# resolve_bssid_by_ssid — интерфейс без MAC: выбор сети по имени
# ---------------------------------------------------------------------------

def test_resolve_bssid_by_ssid_unique_match(conn):
    _seed(conn)
    bssid, candidates = exporter.resolve_bssid_by_ssid(conn, NET_A["ssid"])
    assert bssid == NET_A["bssid"]
    assert candidates == [NET_A["bssid"]]


def test_resolve_bssid_by_ssid_no_match(conn):
    _seed(conn)
    bssid, candidates = exporter.resolve_bssid_by_ssid(conn, "НетТакойСети")
    assert bssid is None
    assert candidates == []


def test_resolve_bssid_by_ssid_collision(conn):
    # Две РАЗНЫЕ физические точки вещают одно и то же имя (частый случай для
    # дефолтных SSID) — резолвер должен вернуть кандидатов, а не угадывать.
    _seed(conn)
    other_bssid = "FF:EE:DD:CC:BB:AA"
    upsert_network(conn, {
        "bssid": other_bssid, "ssid": NET_A["ssid"], "encryption": "Open",
        "manufacturer": "Other", "channel": 1, "frequency": 2412.0,
    })
    conn.commit()

    bssid, candidates = exporter.resolve_bssid_by_ssid(conn, NET_A["ssid"])
    assert bssid is None
    assert sorted(candidates) == sorted([NET_A["bssid"], other_bssid])


# ---------------------------------------------------------------------------
# heatmap_networks — тепловая карта ПО КАЖДОЙ СЕТИ
# ---------------------------------------------------------------------------

def test_heatmap_per_network_one_file_per_network(conn, tmp_path):
    _seed(conn)
    out_dir = str(tmp_path / "heatmap_nets")
    result = exporter.export_heatmap_per_network(conn, out_dir)

    assert result == {"networks": 2, "samples": 3}

    manifest = _read_csv(os.path.join(out_dir, "_manifest.csv"))
    assert manifest[0] == ["bssid", "ssid", "n_samples", "file"]
    assert len(manifest) - 1 == 2

    by_bssid = {row[0]: row for row in manifest[1:]}
    a_file = by_bssid[NET_A["bssid"]][3]
    assert a_file in os.listdir(out_dir)

    a_rows = _read_csv(os.path.join(out_dir, a_file))
    assert len(a_rows) - 1 == 2                      # только сэмплы NetA
    assert all(r[3] == NET_A["bssid"] for r in a_rows[1:])

    b_file = by_bssid[NET_B["bssid"]][3]
    b_rows = _read_csv(os.path.join(out_dir, b_file))
    assert len(b_rows) - 1 == 1                       # у NetB только 1 GPS-наблюдение


def test_heatmap_per_network_sanitizes_filenames(conn, tmp_path):
    _seed(conn)  # NetB Open — SSID с пробелом
    out_dir = str(tmp_path / "heatmap_nets2")
    exporter.export_heatmap_per_network(conn, out_dir)

    files = [f for f in os.listdir(out_dir) if f != "_manifest.csv"]
    assert files
    assert not any(" " in f or ":" in f for f in files)


def test_heatmap_per_network_empty_db(conn, tmp_path):
    out_dir = str(tmp_path / "heatmap_nets_empty")
    result = exporter.export_heatmap_per_network(conn, out_dir)
    assert result == {"networks": 0, "samples": 0}
    assert os.path.exists(os.path.join(out_dir, "_manifest.csv"))


# ---------------------------------------------------------------------------
# ap_status — колонка rtt (не rtt_ms)
# ---------------------------------------------------------------------------

def test_ap_status_csv_header_is_rtt(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "ap_status.csv")
    count = exporter.export_ap_status_csv(conn, out)

    rows = _read_csv(out)
    assert rows[0] == ["ap_id", "timestamp", "status", "rtt", "lat", "lon"]
    assert count == 1
    assert rows[1][3] == "23.5"


# ---------------------------------------------------------------------------
# wigle — WigleWifi-1.4 (сверено с api.wigle.net/csvFormat-1_4.html)
# ---------------------------------------------------------------------------

def test_wigle_csv_format(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "wigle.csv")
    count = exporter.export_wigle_csv(conn, out)

    rows = _read_csv(out)
    assert rows[0][0] == "WigleWifi-1.4"
    assert rows[1] == [
        "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
        "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type",
    ]
    assert count == 2  # одна строка на сеть

    by_mac = {r[0]: r for r in rows[2:]}

    row_a = by_mac[NET_A["bssid"]]
    assert row_a[2] == "[WPA2-PSK][ESS]"
    # first_seen генерируется upsert_network при вставке сети (текущее время),
    # а не берётся из наблюдения — проверяем ТОЛЬКО формат (пробел, без T/Z).
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", row_a[3])
    assert row_a[-1] == "WIFI"

    row_b = by_mac[NET_B["bssid"]]
    assert row_b[2] == "[ESS]"                 # Open → только [ESS]


# ---------------------------------------------------------------------------
# full — CSV (полный, включая записи без GPS)
# ---------------------------------------------------------------------------

def test_full_dataset_csv_includes_no_gps_rows(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "full.csv")
    count = exporter.export_full_dataset_csv(conn, out)

    assert count == 4  # ВСЕ наблюдения, включая без GPS
    rows = _read_csv(out)
    assert len(rows) - 1 == 4


# ---------------------------------------------------------------------------
# full — GeoPackage (структурная проверка через голый sqlite3, без GDAL)
# ---------------------------------------------------------------------------

def _decode_gpkg_point(blob):
    """Разбирает GeoPackage geometry blob (заголовок GP без envelope) → (lon, lat)."""
    wkb = blob[8:]  # magic(2)+version(1)+flags(1)+srs_id(4) = 8-байтный заголовок
    _byte_order, _geom_type = struct.unpack_from("<BI", wkb, 0)
    x, y = struct.unpack_from("<dd", wkb, 5)
    return x, y


def test_full_dataset_gpkg_structure_and_geometry(conn, tmp_path):
    _seed(conn)
    out = str(tmp_path / "full.gpkg")
    count = exporter.export_full_dataset_gpkg(conn, out)
    assert count == 3  # только has_gps=1 (пространственный слой)

    gconn = sqlite3.connect(out)
    try:
        cur = gconn.cursor()

        cur.execute("PRAGMA application_id")
        assert cur.fetchone()[0] == 0x47504B47  # "GPKG"

        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert {"gpkg_contents", "gpkg_geometry_columns", "wifi_observations"} <= tables

        cur.execute(
            "SELECT table_name, geometry_type_name, srs_id FROM gpkg_geometry_columns"
        )
        assert cur.fetchone() == ("wifi_observations", "POINT", 4326)

        cur.execute("SELECT bssid, rssi, shape FROM wifi_observations")
        rows = cur.fetchall()
        assert len(rows) == 3

        by_rssi = {rssi: (bssid, blob) for bssid, rssi, blob in rows}
        bssid, blob = by_rssi[-50]
        lon, lat = _decode_gpkg_point(blob)
        assert bssid == NET_A["bssid"]
        assert lon == pytest.approx(39.7188)
        assert lat == pytest.approx(47.2225)
    finally:
        gconn.close()


def test_full_dataset_gpkg_empty_db_creates_valid_file(conn, tmp_path):
    out = str(tmp_path / "empty.gpkg")
    count = exporter.export_full_dataset_gpkg(conn, out)
    assert count == 0
    assert os.path.exists(out)


# ---------------------------------------------------------------------------
# full — оба файла одновременно
# ---------------------------------------------------------------------------

def test_full_dataset_creates_both_files(conn, tmp_path):
    _seed(conn)
    out_base = str(tmp_path / "full_both")
    result = exporter.export_full_dataset(conn, out_base)

    assert result == {"gpkg": 3, "csv": 4}
    assert os.path.exists(out_base + ".gpkg")
    assert os.path.exists(out_base + ".csv")


# ---------------------------------------------------------------------------
# Хелперы форматирования Wigle (юнит-уровень)
# ---------------------------------------------------------------------------

def test_wigle_time_helper():
    assert exporter._wigle_time("2026-06-02T12:00:00Z") == "2026-06-02 12:00:00"
    assert exporter._wigle_time("") == ""
    assert exporter._wigle_time(None) == ""


def test_wigle_authmode_helper():
    assert exporter._wigle_authmode("WPA2-PSK") == "[WPA2-PSK][ESS]"
    assert exporter._wigle_authmode("Open") == "[ESS]"
    assert exporter._wigle_authmode(None) == "[ESS]"
    assert exporter._wigle_authmode("WEP") == "[WEP][ESS]"


def test_sanitize_filename_helper():
    assert exporter._sanitize_filename("MyNet Garage:01") == "MyNet_Garage_01"
    assert exporter._sanitize_filename("") == "hidden"
    assert exporter._sanitize_filename(None) == "hidden"


# ---------------------------------------------------------------------------
# normalize_time_bound — разбор --since/--until
# ---------------------------------------------------------------------------

def test_normalize_time_bound_date_only_start_of_day():
    assert exporter.normalize_time_bound("2026-07-23") == "2026-07-23T00:00:00Z"


def test_normalize_time_bound_date_only_end_of_day():
    assert exporter.normalize_time_bound("2026-07-23", end_of_day=True) == "2026-07-23T23:59:59Z"


def test_normalize_time_bound_full_datetime_passthrough():
    assert exporter.normalize_time_bound("2026-07-23T14:30:00") == "2026-07-23T14:30:00Z"
    assert exporter.normalize_time_bound("2026-07-23T14:30:00Z") == "2026-07-23T14:30:00Z"


def test_normalize_time_bound_none_and_empty():
    assert exporter.normalize_time_bound(None) is None
    assert exporter.normalize_time_bound("") is None


def test_normalize_time_bound_invalid_raises():
    with pytest.raises(ValueError):
        exporter.normalize_time_bound("не дата")
    with pytest.raises(ValueError):
        exporter.normalize_time_bound("23.07.2026")


# ---------------------------------------------------------------------------
# since/until — сужение экспорта до конкретного дня (все профили)
# ---------------------------------------------------------------------------

def _day_bounds(day):
    return exporter.normalize_time_bound(day), exporter.normalize_time_bound(day, end_of_day=True)


def test_heatmap_csv_since_until_selects_single_day(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out = str(tmp_path / "heatmap_day2.csv")
    count = exporter.export_heatmap_csv(conn, out, since=since, until=until)

    assert count == 2  # NetA (день2) + NetB (день2); NetC (только день1) исключён
    rows = _read_csv(out)
    bssids = {r[3] for r in rows[1:]}
    assert bssids == {NET_A["bssid"], NET_B["bssid"]}


def test_heatmap_csv_since_until_combined_with_bssid_filter(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY1)
    out = str(tmp_path / "heatmap_a_day1.csv")
    count = exporter.export_heatmap_csv(
        conn, out, bssid_filter=NET_A["bssid"], since=since, until=until,
    )
    assert count == 2  # оба наблюдения NetA за день1; NetC отфильтрован по bssid


def test_heatmap_per_network_since_until(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out_dir = str(tmp_path / "heatmap_nets_day2")
    result = exporter.export_heatmap_per_network(conn, out_dir, since=since, until=until)

    assert result == {"networks": 2, "samples": 2}  # NetC (день1) не входит
    manifest = _read_csv(os.path.join(out_dir, "_manifest.csv"))
    manifest_bssids = {row[0] for row in manifest[1:]}
    assert manifest_bssids == {NET_A["bssid"], NET_B["bssid"]}


def test_full_dataset_csv_since_until(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY1)
    out = str(tmp_path / "full_day1.csv")
    count = exporter.export_full_dataset_csv(conn, out, since=since, until=until)
    assert count == 3  # 2×NetA + 1×NetC за день1; день2 исключён


def test_full_dataset_gpkg_since_until(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out = str(tmp_path / "full_day2.gpkg")
    count = exporter.export_full_dataset_gpkg(conn, out, since=since, until=until)
    assert count == 2  # NetA + NetB за день2


def test_full_dataset_since_until_both_files(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out_base = str(tmp_path / "full_day2_both")
    result = exporter.export_full_dataset(conn, out_base, since=since, until=until)
    assert result == {"gpkg": 2, "csv": 2}


def test_ap_status_csv_since_until(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out = str(tmp_path / "ap_status_day2.csv")
    count = exporter.export_ap_status_csv(conn, out, since=since, until=until)

    assert count == 1
    rows = _read_csv(out)
    assert rows[1][2] == "no_inet"  # запись именно за день2


def test_wigle_csv_since_until_excludes_network_outside_window(conn, tmp_path):
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out = str(tmp_path / "wigle_day2.csv")
    count = exporter.export_wigle_csv(conn, out, since=since, until=until)

    rows = _read_csv(out)
    macs = {r[0] for r in rows[2:]}
    # NetC наблюдалась только в день1 — в окне дня2 её не должно быть вовсе
    assert NET_C["bssid"] not in macs
    assert macs == {NET_A["bssid"], NET_B["bssid"]}
    assert count == 2


def test_wigle_csv_since_until_uses_observation_within_window(conn, tmp_path):
    # Без окна "первое наблюдение" NetA было бы за день1 (lat=10.0);
    # с окном дня2 должно подставиться наблюдение ИЗ этого окна (lat=30.0).
    _seed_multi_day(conn)
    since, until = _day_bounds(DAY2)
    out = str(tmp_path / "wigle_day2_coords.csv")
    exporter.export_wigle_csv(conn, out, since=since, until=until)

    rows = _read_csv(out)
    row_a = next(r for r in rows[2:] if r[0] == NET_A["bssid"])
    assert float(row_a[6]) == pytest.approx(30.0)  # CurrentLatitude — из дня2, не дня1


def test_wigle_csv_without_range_includes_all_networks_left_join(conn, tmp_path):
    # Без --since/--until поведение НЕ меняется: все сети (в т.ч. NetC), LEFT JOIN.
    _seed_multi_day(conn)
    out = str(tmp_path / "wigle_all.csv")
    count = exporter.export_wigle_csv(conn, out)

    rows = _read_csv(out)
    macs = {r[0] for r in rows[2:]}
    assert macs == {NET_A["bssid"], NET_B["bssid"], NET_C["bssid"]}
    assert count == 3
