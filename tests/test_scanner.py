# -*- coding: utf-8 -*-
"""
tests/test_scanner.py — тесты разбора скана и нормализации типа шифрования.
"""

from src import scanner
from src.scanner import (
    SEC_OPEN, SEC_WPA, SEC_WPA2, SEC_WPA3, SEC_EAP, SEC_WEP,
    parse_security, parse_scan, normalize_security, strongest_by_ssid,
)


# ---------------------------------------------------------------------------
# parse_security — по capability-блоку iw scan
# ---------------------------------------------------------------------------

def test_parse_security_open():
    lines = ["\tcapability: ESS (0x0021)"]
    assert parse_security(lines) == SEC_OPEN


def test_parse_security_wep():
    # Privacy без RSN/WPA — WEP
    lines = ["\tcapability: ESS Privacy (0x0411)"]
    assert parse_security(lines) == SEC_WEP


def test_parse_security_wpa2_psk():
    lines = [
        "\tRSN:\t * Version: 1",
        "\t\t * Pairwise ciphers: CCMP",
        "\t\t * Authentication suites: PSK",
    ]
    assert parse_security(lines) == SEC_WPA2


def test_parse_security_wpa3_sae_only():
    lines = [
        "\tRSN:\t * Version: 1",
        "\t\t * Authentication suites: SAE",
    ]
    assert parse_security(lines) == SEC_WPA3


def test_parse_security_wpa3_transition_is_wpa2():
    # PSK + SAE (transition) → для совместимости трактуем как wpa2-psk
    lines = [
        "\tRSN:\t * Version: 1",
        "\t\t * Authentication suites: PSK SAE",
    ]
    assert parse_security(lines) == SEC_WPA2


def test_parse_security_enterprise_eap():
    lines = [
        "\tRSN:\t * Version: 1",
        "\t\t * Authentication suites: IEEE 802.1X",
    ]
    assert parse_security(lines) == SEC_EAP


def test_parse_security_old_wpa1():
    lines = [
        "\tWPA:\t * Version: 1",
        "\t\t * Authentication suites: PSK",
    ]
    assert parse_security(lines) == SEC_WPA


# ---------------------------------------------------------------------------
# normalize_security — из строки Kismet (encryption)
# ---------------------------------------------------------------------------

def test_normalize_security_variants():
    assert normalize_security("WPA2-PSK") == SEC_WPA2
    assert normalize_security("WPA3-SAE") == SEC_WPA3
    assert normalize_security("WPA2-PSK-SAE") == SEC_WPA2   # transition
    assert normalize_security("WPA2-EAP") == SEC_EAP
    assert normalize_security("Open") == SEC_OPEN
    assert normalize_security("WEP") == SEC_WEP
    assert normalize_security("WPA-PSK") == SEC_WPA


def test_normalize_security_unknown_and_empty():
    assert normalize_security("") is None
    assert normalize_security(None) is None
    assert normalize_security("нечто") is None


# ---------------------------------------------------------------------------
# parse_scan — полный вывод iw scan
# ---------------------------------------------------------------------------

_IW_SCAN = """\
BSS aa:bb:cc:dd:ee:01(on wlan1)
	freq: 2437
	signal: -42.00 dBm
	SSID: MyNet_Garage
	RSN:	 * Version: 1
		 * Pairwise ciphers: CCMP
		 * Authentication suites: PSK
	capability: ESS Privacy (0x0411)
BSS aa:bb:cc:dd:ee:02(on wlan1)
	freq: 5180
	signal: -71.00 dBm
	SSID: Guest3
	RSN:	 * Version: 1
		 * Authentication suites: SAE
	capability: ESS Privacy (0x0411)
BSS aa:bb:cc:dd:ee:03(on wlan1)
	freq: 2412
	signal: -80.00 dBm
	SSID:
	capability: ESS (0x0021)
"""


def test_parse_scan_basic():
    nets = parse_scan(_IW_SCAN)
    assert len(nets) == 3

    n0 = nets[0]
    assert n0["bssid"] == "aa:bb:cc:dd:ee:01"
    assert n0["ssid"] == "MyNet_Garage"
    assert n0["signal"] == -42
    assert n0["freq"] == 2437
    assert n0["security"] == SEC_WPA2

    n1 = nets[1]
    assert n1["ssid"] == "Guest3"
    assert n1["security"] == SEC_WPA3

    # третья — скрытая (пустой SSID), open
    assert nets[2]["ssid"] == ""
    assert nets[2]["security"] == SEC_OPEN


def test_parse_scan_empty():
    assert parse_scan("") == []
    assert parse_scan(None) == []


# ---------------------------------------------------------------------------
# strongest_by_ssid
# ---------------------------------------------------------------------------

def test_strongest_by_ssid_picks_max_and_drops_hidden():
    nets = [
        {"ssid": "A", "bssid": "m1", "signal": -70, "security": SEC_WPA2},
        {"ssid": "A", "bssid": "m2", "signal": -50, "security": SEC_WPA2},  # сильнее
        {"ssid": "",  "bssid": "m3", "signal": -40, "security": SEC_OPEN},  # скрытая — пропуск
    ]
    result = strongest_by_ssid(nets)
    assert set(result.keys()) == {"A"}
    assert result["A"]["signal"] == -50
    assert result["A"]["bssid"] == "m2"
