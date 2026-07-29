# -*- coding: utf-8 -*-
"""
tests/test_ui.py — рендереры src/ui.py не падают и выдают ожидаемое содержимое.

Console перенаправляется в буфер (force_terminal, чтобы markup реально
раскрашивался, а не оставался как есть), чтобы проверить и текст, и то, что
rich-разметка действительно обрабатывается (есть ANSI escape-последовательности).
"""

import io

from rich.console import Console

from src import ui


def _capture():
    """Возвращает (buf, console) — console пишет в buf с включённым цветом."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=100)
    return buf, console


def test_adapters_table_renders_expected_fields():
    buf, console = _capture()
    adapters = [
        {
            "iface": "wlan0", "mac": "AA:BB:CC:DD:EE:FF",
            "driver": "rtl8812au", "chipset": "Realtek RTL8812AU",
            "supports_monitor": True, "usb_path": "usb1/1-1.3",
        },
        {
            "iface": "wlan1", "mac": "11:22:33:44:55:66",
            "driver": "ath9k_htc", "chipset": "unknown",
            "supports_monitor": False, "usb_path": None,
        },
    ]
    console.print(ui.adapters_table(adapters))
    out = buf.getvalue()

    assert "wlan0" in out
    assert "AA:BB:CC:DD:EE:FF" in out
    assert "wlan1" in out
    assert "✓" in out
    assert "✗" in out
    assert chr(27) in out  # ANSI escape — markup реально обработан, не выведен буквально


def test_adapters_table_empty_list_does_not_crash():
    buf, console = _capture()
    console.print(ui.adapters_table([]))
    assert buf.getvalue()  # хотя бы заголовок таблицы напечатан


def test_scan_results_table_renders_rows():
    buf, console = _capture()
    rows = [
        {"bssid": "AA:BB:CC:DD:EE:01", "ssid": "NetA", "encryption": "WPA2-PSK",
         "channel": 6, "lat": 47.2225, "lon": 39.7188},
        {"bssid": "AA:BB:CC:DD:EE:02", "ssid": "NetB", "encryption": None,
         "channel": 11, "lat": None, "lon": None},
    ]
    console.print(ui.scan_results_table(rows))
    out = buf.getvalue()

    assert "NetA" in out
    assert "NetB" in out
    assert "No GPS" in out  # нет координат у NetB


def test_ap_result_all_known_statuses_do_not_crash():
    _, console = _capture()
    original_console = ui.console
    ui.console = console
    try:
        for status in (
            "ok", "no_assoc", "no_dhcp", "no_dns", "no_inet",
            "timeout", "captive_portal", "eap_unsupported", "error",
        ):
            ui.ap_result("ap-1", status, rtt_ms=23.5, ssid="MyNet")
        ui.ap_result("ap-2", "ok", rtt_ms=None)          # без RTT — не должно падать
        ui.ap_result("ap-3", "unknown_status_xyz")       # неизвестный статус — не должно падать
    finally:
        ui.console = original_console


def test_ap_result_output_contains_status_and_rtt():
    buf, console = _capture()
    original_console = ui.console
    ui.console = console
    try:
        ui.ap_result("ap-garage-01", "ok", rtt_ms=23.5, ssid="MyNet_Garage")
    finally:
        ui.console = original_console

    out = buf.getvalue()
    assert "ap-garage-01" in out
    assert "ok" in out
    assert "мс" in out  # RTT напечатан (23.5 -> "{:.0f}" округляет до "24")


def test_export_summary_table_renders_rows():
    buf, console = _capture()
    items = [
        ("heatmap", "123 строк", "export/heatmap_20260721.csv"),
        ("full", "3 точек", "export/full_20260721.gpkg"),
        ("full", "4 строк", "export/full_20260721.csv"),
    ]
    console.print(ui.export_summary_table(items))
    out = buf.getvalue()

    assert "heatmap" in out
    assert "full" in out
    assert "123" in out


def test_spinner_is_usable_as_context_manager():
    original_console = ui.console
    buf, console = _capture()
    ui.console = console
    try:
        with ui.spinner("Тест..."):
            pass  # спиннер должен корректно открыться и закрыться без исключений
    finally:
        ui.console = original_console
