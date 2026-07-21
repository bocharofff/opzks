# -*- coding: utf-8 -*-
"""
tests/test_ap_presence.py — цикл проверки точек «по присутствию» (src.cli.ap_check_loop).

Проверяем: матчинг по SSID, порядок «сильнейший первым», проброс observed_security,
кулдаун перепроверки, порог min_signal, отсутствующие цели не проверяются.
"""

import threading

import pytest

from src.cli import ap_check_loop
from src.db import init_db


class FakeChecker:
    """Записывает вызовы check_one; результат — минимальный dict для ap_health."""

    def __init__(self, stop_after=None, stop_event=None):
        self.calls = []                 # list of (ap_id, ssid, observed_security)
        self._stop_after = stop_after
        self._stop_event = stop_event

    def check_one(self, ap, observed_security=None):
        self.calls.append((ap.get("id"), ap.get("ssid"), observed_security))
        if self._stop_after is not None and len(self.calls) >= self._stop_after:
            self._stop_event.set()
        return {
            "ap_id": ap.get("id", "unknown"),
            "status": "ok",
            "rtt_ms": 12.0,
            "lat": None,
            "lon": None,
        }


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "ap.db"))
    yield c
    c.close()


def _targets():
    return [
        {"id": "ap-a", "ssid": "NetA"},
        {"id": "ap-b", "ssid": "NetB"},
    ]


def test_matches_by_ssid_strongest_first_and_passes_security(conn):
    stop = threading.Event()
    checker = FakeChecker(stop_after=2, stop_event=stop)

    visible = {
        "NetB": {"bssid": "b", "signal": -70, "security": "wpa2-psk"},
        "NetA": {"bssid": "a", "signal": -40, "security": "wpa3-sae"},
        "Alien": {"bssid": "x", "signal": -30, "security": "open"},  # чужая
    }

    ap_check_loop(
        checker, conn, stop, presence_fn=lambda: visible, targets=_targets(),
        recheck_interval=1000, scan_interval=0,
    )

    ids = [c[0] for c in checker.calls]
    assert ids == ["ap-a", "ap-b"]                 # сильнейший (NetA -40) первым
    assert ("ap-a", "NetA", "wpa3-sae") in checker.calls   # observed_security проброшен
    assert "Alien" not in [c[1] for c in checker.calls]    # чужая не проверяется

    n = conn.execute("SELECT COUNT(*) AS n FROM ap_health").fetchone()["n"]
    assert n == 2


def test_absent_target_never_checked(conn):
    stop = threading.Event()
    checker = FakeChecker()

    calls = {"n": 0}

    def presence():
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        return {"Alien": {"bssid": "x", "signal": -40, "security": "open"}}

    ap_check_loop(
        checker, conn, stop, presence_fn=presence, targets=_targets(),
        recheck_interval=1000, scan_interval=0,
    )
    assert checker.calls == []                      # ни одной цели не видно → нет проверок


def test_cooldown_blocks_recheck(conn):
    stop = threading.Event()
    checker = FakeChecker()

    calls = {"n": 0}

    def presence():
        calls["n"] += 1
        if calls["n"] >= 4:                         # даём 3 итерации с видимой целью
            stop.set()
            return {}
        return {"NetA": {"bssid": "a", "signal": -40, "security": "wpa2-psk"}}

    ap_check_loop(
        checker, conn, stop, presence_fn=presence, targets=_targets(),
        recheck_interval=1000, scan_interval=0,     # большой кулдаун
    )
    # Несмотря на 3 итерации присутствия — проверка ровно одна (кулдаун держит)
    assert len(checker.calls) == 1
    assert checker.calls[0][0] == "ap-a"


def test_min_signal_threshold_skips_weak(conn):
    stop = threading.Event()
    checker = FakeChecker()

    calls = {"n": 0}

    def presence():
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        return {"NetA": {"bssid": "a", "signal": -90, "security": "wpa2-psk"}}

    ap_check_loop(
        checker, conn, stop, presence_fn=presence, targets=_targets(),
        recheck_interval=1000, scan_interval=0, min_signal=-80,
    )
    assert checker.calls == []                      # -90 < -80 → слишком слабо, пропуск
