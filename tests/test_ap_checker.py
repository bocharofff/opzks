# -*- coding: utf-8 -*-
"""
tests/test_ap_checker.py — резолв типа шифрования, генерация wpa_supplicant.conf,
статус eap_unsupported, отсутствие пароля в результате.
"""

import os

from src.ap_checker import APChecker


def _checker(default_security="wpa2-psk"):
    # conn/gps не нужны для тестируемых методов
    return APChecker(
        iface="wlan1",
        targets_path="config/ap_targets.yaml",
        conn=None,
        gps=None,
        default_security=default_security,
    )


# ---------------------------------------------------------------------------
# _resolve_security — приоритет: конфиг > наблюдение > дефолт
# ---------------------------------------------------------------------------

def test_resolve_security_explicit_wins():
    c = _checker()
    ap = {"ssid": "N", "security": "wpa3-sae"}
    assert c._resolve_security(ap, observed_security="wpa2-psk") == "wpa3-sae"


def test_resolve_security_observed_used_when_no_explicit():
    c = _checker()
    ap = {"ssid": "N"}
    assert c._resolve_security(ap, observed_security="wpa3-sae") == "wpa3-sae"


def test_resolve_security_falls_back_to_default():
    c = _checker(default_security="wpa2-psk")
    ap = {"ssid": "N"}
    assert c._resolve_security(ap, observed_security=None) == "wpa2-psk"


# ---------------------------------------------------------------------------
# _write_wpa_conf — корректный key_mgmt и отсутствие bssid
# ---------------------------------------------------------------------------

def _conf_text(ap, security):
    c = _checker()
    path = c._write_wpa_conf(ap, security)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_write_conf_wpa2_psk():
    text = _conf_text({"ssid": "MyNet", "psk": "secretpass"}, "wpa2-psk")
    assert 'ssid="MyNet"' in text
    assert "key_mgmt=WPA-PSK" in text
    assert "proto=RSN" in text
    assert 'psk="secretpass"' in text
    assert "bssid=" not in text          # BSSID больше не используется


def test_write_conf_wpa3_sae():
    text = _conf_text({"ssid": "MyNet", "psk": "secretpass"}, "wpa3-sae")
    assert "key_mgmt=SAE" in text
    assert "ieee80211w=2" in text


def test_write_conf_open_has_no_psk():
    text = _conf_text({"ssid": "FreeWifi"}, "open")
    assert "key_mgmt=NONE" in text
    assert "psk=" not in text


def test_write_conf_psk_hash_preferred_over_psk():
    ap = {"ssid": "MyNet", "psk": "plain", "psk_hash": "abcdef0123456789"}
    text = _conf_text(ap, "wpa2-psk")
    assert "psk=abcdef0123456789" in text   # хеш без кавычек
    assert 'psk="plain"' not in text


# ---------------------------------------------------------------------------
# check_one — Enterprise не поддержан; пароль не в результате
# ---------------------------------------------------------------------------

def test_check_one_eap_unsupported_no_attempt():
    c = _checker()
    ap = {"id": "ap-eap", "ssid": "CorpNet", "psk": "x"}
    result = c.check_one(ap, observed_security="wpa2-eap")
    assert result["status"] == "eap_unsupported"
    assert result["ap_id"] == "ap-eap"
    assert result["rtt_ms"] is None


def test_check_one_result_never_contains_password():
    c = _checker()
    ap = {"id": "ap-eap", "ssid": "CorpNet", "psk": "s3cr3t", "psk_hash": "deadbeef"}
    result = c.check_one(ap, observed_security="wpa2-eap")
    assert "psk" not in result
    assert "psk_hash" not in result
    assert "s3cr3t" not in str(result)
