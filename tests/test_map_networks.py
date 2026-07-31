"""Тесты группировки по BSSID и правила именования (FR-2, FR-3)."""

import pandas as pd

from src.wifi_heatmap.networks import build_network_index, format_network_table, select_networks


def _df(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["lat", "lon", "rssi", "bssid", "ssid", "timestamp"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def test_ssid_disambiguation_by_first_seen():
    rows = [
        [55.0, 37.0, -50, "AA:AA:AA:AA:AA:01", "Home", "2026-07-21T10:00:00Z"],
        [55.0, 37.0, -50, "AA:AA:AA:AA:AA:02", "Home", "2026-07-21T09:00:00Z"],  # раньше -> базовое имя
    ]
    idx = build_network_index(_df(rows))
    assert idx["AA:AA:AA:AA:AA:02"].display_name == "Home"
    assert idx["AA:AA:AA:AA:AA:01"].display_name == "Home (1)"


def test_tie_break_by_bssid_when_first_seen_equal():
    rows = [
        [55.0, 37.0, -50, "BB:BB:BB:BB:BB:BB", "Same", "2026-07-21T10:00:00Z"],
        [55.0, 37.0, -50, "AA:AA:AA:AA:AA:AA", "Same", "2026-07-21T10:00:00Z"],
    ]
    idx = build_network_index(_df(rows))
    assert idx["AA:AA:AA:AA:AA:AA"].display_name == "Same"
    assert idx["BB:BB:BB:BB:BB:BB"].display_name == "Same (1)"


def test_hidden_network_uses_bssid_as_name():
    rows = [
        [55.0, 37.0, -50, "02:99:47:A6:00:A0", "02:99:47:A6:00:A0", "2026-07-21T10:00:00Z"],
        [55.0, 37.0, -50, "02:99:47:A6:00:A1", "", "2026-07-21T10:00:01Z"],
    ]
    idx = build_network_index(_df(rows))
    assert idx["02:99:47:A6:00:A0"].display_name == "02:99:47:A6:00:A0"
    assert idx["02:99:47:A6:00:A1"].display_name == "02:99:47:A6:00:A1"


def test_select_networks_top_and_min_points():
    rows = []
    for i in range(25):  # сеть A: 25 точек
        rows.append([55.0, 37.0, -50, "AA:AA:AA:AA:AA:AA", "A", f"2026-07-21T10:00:{i:02d}Z"])
    for i in range(10):  # сеть B: 10 точек — не пройдёт --min-points=20
        rows.append([55.0, 37.0, -50, "BB:BB:BB:BB:BB:BB", "B", f"2026-07-21T11:00:{i:02d}Z"])
    for i in range(30):  # сеть C: 30 точек
        rows.append([55.0, 37.0, -50, "CC:CC:CC:CC:CC:CC", "C", f"2026-07-21T12:00:{i:02d}Z"])

    idx = build_network_index(_df(rows))
    sel = select_networks(idx, top=1, min_points=20)

    assert len(sel.networks) == 1
    assert sel.networks[0].bssid == "CC:CC:CC:CC:CC:CC"
    assert any("Пропущено 1" in w for w in sel.warnings)


def test_select_networks_explicit_bssid_bypasses_min_points():
    rows = [[55.0, 37.0, -50, "AA:AA:AA:AA:AA:AA", "A", "2026-07-21T10:00:00Z"]]
    idx = build_network_index(_df(rows))
    sel = select_networks(idx, bssids=["aa:aa:aa:aa:aa:aa"], top=10, min_points=20)
    assert len(sel.networks) == 1
    assert sel.networks[0].bssid == "AA:AA:AA:AA:AA:AA"
    assert any("меньше --min-points" in w for w in sel.warnings)


def test_select_networks_unknown_bssid_warns():
    rows = [[55.0, 37.0, -50, "AA:AA:AA:AA:AA:AA", "A", "2026-07-21T10:00:00Z"]]
    idx = build_network_index(_df(rows))
    sel = select_networks(idx, bssids=["FF:FF:FF:FF:FF:FF"], top=10, min_points=1)
    assert len(sel.networks) == 0
    assert any("не найден" in w for w in sel.warnings)


def test_format_network_table_empty():
    assert "не найдены" in format_network_table([])
