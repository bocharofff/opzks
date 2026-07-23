# -*- coding: utf-8 -*-
"""
tests/test_ap_checker.py — резолв типа шифрования, сборка команды nmcli
connection add, классификация причин отказа NetworkManager, статус
eap_unsupported, отсутствие пароля в результате.
"""

from src.ap_checker import APChecker, _build_nmcli_add_cmd, classify_nm_failure


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
# _build_nmcli_add_cmd — корректный key-mgmt, отсутствие bssid, приоритет
# psk_hash над psk, hidden-флаг
# ---------------------------------------------------------------------------

def _arg_after(cmd, key):
    """Возвращает значение, следующее сразу за токеном ``key`` в списке argv."""
    idx = cmd.index(key)
    return cmd[idx + 1]


def test_nmcli_add_cmd_wpa2_psk():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "MyNet", "psk": "secretpass"}, "wpa2-psk")
    assert _arg_after(cmd, "ssid") == "MyNet"
    assert _arg_after(cmd, "ifname") == "wlan1"
    assert _arg_after(cmd, "con-name") == "con1"
    assert _arg_after(cmd, "wifi-sec.key-mgmt") == "wpa-psk"
    assert _arg_after(cmd, "wifi-sec.psk") == "secretpass"
    assert "bssid" not in cmd            # BSSID не используется вовсе
    assert _arg_after(cmd, "connection.autoconnect") == "no"


def test_nmcli_add_cmd_wpa3_sae():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "MyNet", "psk": "secretpass"}, "wpa3-sae")
    assert _arg_after(cmd, "wifi-sec.key-mgmt") == "sae"
    assert _arg_after(cmd, "wifi-sec.psk") == "secretpass"


def test_nmcli_add_cmd_open_has_no_security_args():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "FreeWifi"}, "open")
    assert "wifi-sec.key-mgmt" not in cmd
    assert "wifi-sec.psk" not in cmd


def test_nmcli_add_cmd_wep():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "OldNet", "psk": "abcde"}, "wep")
    assert _arg_after(cmd, "wifi-sec.key-mgmt") == "none"
    assert _arg_after(cmd, "wifi-sec.wep-key0") == "abcde"


def test_nmcli_add_cmd_psk_hash_preferred_over_psk():
    ap = {"ssid": "MyNet", "psk": "plain", "psk_hash": "abcdef0123456789"}
    cmd = _build_nmcli_add_cmd("con1", "wlan1", ap, "wpa2-psk")
    assert _arg_after(cmd, "wifi-sec.psk") == "abcdef0123456789"


def test_nmcli_add_cmd_hidden_flag():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "Hidden", "hidden": True}, "wpa2-psk")
    assert _arg_after(cmd, "802-11-wireless.hidden") == "yes"


def test_nmcli_add_cmd_not_hidden_by_default():
    cmd = _build_nmcli_add_cmd("con1", "wlan1", {"ssid": "Visible"}, "wpa2-psk")
    assert "802-11-wireless.hidden" not in cmd


# ---------------------------------------------------------------------------
# classify_nm_failure — причины NetworkManager → no_dhcp / no_assoc
# ---------------------------------------------------------------------------

def test_classify_dhcp_related_reasons():
    for reason in (
        "ip-config-unavailable", "ip-config-expired",
        "dhcp-start-failed", "dhcp-error", "dhcp-failed",
        "IP_CONFIG_UNAVAILABLE",  # регистр/подчёркивания не важны
    ):
        assert classify_nm_failure(reason) == "no_dhcp", reason


def test_classify_assoc_related_reasons_default_to_no_assoc():
    for reason in ("no-secrets", "supplicant-disconnect", "supplicant-timeout", "config-failed"):
        assert classify_nm_failure(reason) == "no_assoc", reason


def test_classify_unknown_reason_defaults_to_no_assoc():
    assert classify_nm_failure("something-completely-unrecognized") == "no_assoc"
    assert classify_nm_failure(None) == "no_assoc"
    assert classify_nm_failure("") == "no_assoc"


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
