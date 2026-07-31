"""Тесты загрузки и валидации CSV (FR-1)."""

import pytest

from src.wifi_heatmap.loader import MAC_RE, LoaderError, load_measurements


def test_valid_row_passes(csv_path):
    path = csv_path(["55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z"])
    df, report = load_measurements(path)
    assert len(df) == 1
    assert report.rows_valid == 1
    assert report.unique_bssids == 1


def test_missing_coords_dropped(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        ",37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:49Z",  # пустая широта
        "0,0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:50Z",  # ровно (0,0)
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.dropped["missing_coords"] == 2


def test_bad_coords_dropped(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "999,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:49Z",  # широта вне диапазона
        "55.0,-999,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:50Z",  # долгота вне диапазона
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.dropped["bad_coords"] == 2


def test_bad_rssi_dropped(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "55.0,37.0,50,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:49Z",  # вне -100..0
        "55.0,37.0,notanumber,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:50Z",
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.dropped["bad_rssi"] == 2


def test_bad_bssid_dropped(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "55.0,37.0,-70,not-a-mac,Test,2026-07-21T16:49:49Z",
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.dropped["bad_bssid"] == 1


def test_bad_timestamp_dropped(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,not-a-timestamp",
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.dropped["bad_timestamp"] == 1


def test_priority_order_counts_row_once(csv_path):
    # строка проваливает и bad_coords, и bad_rssi одновременно — засчитывается только по bad_coords
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "999,37.0,999,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:49Z",
    ]
    df, report = load_measurements(csv_path(rows))
    assert report.dropped["bad_coords"] == 1
    assert report.dropped["bad_rssi"] == 0
    assert sum(report.dropped.values()) == 1


def test_bssid_normalized_and_ssid_trimmed(csv_path):
    rows = ["55.0,37.0,-70,aa:bb:cc:dd:ee:ff,  Galaxy S24 Ultra  ,2026-07-21T16:49:48Z"]
    df, _ = load_measurements(csv_path(rows))
    assert df.iloc[0]["bssid"] == "AA:BB:CC:DD:EE:FF"
    assert df.iloc[0]["ssid"] == "Galaxy S24 Ultra"


def test_exact_duplicates_removed(csv_path):
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",  # точный дубль
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:49Z",  # другой timestamp — не дубль
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 2
    assert report.duplicates_removed == 1


def test_duplicate_key_ignores_rssi(csv_path):
    # bssid+timestamp+lat+lon совпадают, rssi разный — по FR-1 ключ дедупа не включает rssi
    rows = [
        "55.0,37.0,-70,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
        "55.0,37.0,-71,AA:BB:CC:DD:EE:FF,Test,2026-07-21T16:49:48Z",
    ]
    df, report = load_measurements(csv_path(rows))
    assert len(df) == 1
    assert report.duplicates_removed == 1


def test_missing_file_raises():
    with pytest.raises(LoaderError):
        load_measurements("this/path/does/not/exist.csv")


def test_missing_columns_raises(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(LoaderError):
        load_measurements(str(path))


def test_no_valid_rows_raises(csv_path):
    rows = ["999,999,-70,not-a-mac,Test,not-a-timestamp"]
    with pytest.raises(LoaderError):
        load_measurements(csv_path(rows))


def test_mac_regex():
    assert MAC_RE.fullmatch("AA:BB:CC:DD:EE:FF")
    assert not MAC_RE.fullmatch("AA:BB:CC:DD:EE:FG")
    assert not MAC_RE.fullmatch("AABBCCDDEEFF")
