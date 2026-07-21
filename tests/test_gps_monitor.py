# -*- coding: utf-8 -*-
"""
tests/test_gps_monitor.py — тесты GPSMonitor.position_at (координата по времени пакета).
"""

from src.gps_monitor import GPSMonitor


def _monitor_with_track(points):
    """GPSMonitor с заранее наполненным треком (без подключения к gpsd)."""
    mon = GPSMonitor()
    for ts, lat, lon in points:
        mon._track.append((ts, lat, lon))
    return mon


def test_position_at_nearest_within_tolerance():
    mon = _monitor_with_track([
        (100.0, 1.0, 2.0),
        (101.0, 1.1, 2.1),
        (105.0, 1.5, 2.5),
    ])
    pos = mon.position_at(101.2, tol=2.0)
    assert pos == {"lat": 1.1, "lon": 2.1}


def test_position_at_picks_by_time_not_latest():
    # Последний фикс — 105.0, но для ts=101.2 должна выбраться позиция 101.0
    mon = _monitor_with_track([
        (100.0, 1.0, 2.0),
        (101.0, 1.1, 2.1),
        (105.0, 9.9, 9.9),  # «сейчас» — но не по времени пакета
    ])
    pos = mon.position_at(101.2, tol=2.0)
    assert pos == {"lat": 1.1, "lon": 2.1}


def test_position_at_outside_tolerance_returns_none():
    mon = _monitor_with_track([
        (100.0, 1.0, 2.0),
        (105.0, 1.5, 2.5),
    ])
    # Ближайший к 103.0 — на расстоянии 2 c, а допуск 1 c → None
    assert mon.position_at(103.0, tol=1.0) is None


def test_position_at_none_input_and_empty_track():
    mon = _monitor_with_track([])
    assert mon.position_at(None) is None
    assert mon.position_at(100.0) is None
