# -*- coding: utf-8 -*-
"""
tests/test_map_export.py — профиль экспорта ``map``: HTML-карта прямо из SQLite.

Заменяет smoke-тесты CLI исходного модуля (его CLI не переносился — вместо него
интеграция в наш экспорт). Проверяет сквозной путь БД → валидация → интерполяция →
HTML, фильтры (сеть/период), понятные ошибки и подхват настроек из секции ``map``.
"""

import os

import pytest

from src import cli, exporter
from src.db import insert_observation, upsert_network

NET_A = {
    "bssid": "AA:BB:CC:DD:EE:01", "ssid": "NetA", "encryption": "WPA2-PSK",
    "manufacturer": "Alfa", "channel": 6, "frequency": 2437.0,
}
NET_B = {
    "bssid": "AA:BB:CC:DD:EE:02", "ssid": "NetB", "encryption": "Open",
    "manufacturer": "TP-Link", "channel": 11, "frequency": 2462.0,
}

DAY1 = "2026-07-20"
DAY2 = "2026-07-23"


def _track(conn, bssid, day, n, lat0=55.7000, lon0=37.6000, rssi0=-55):
    """Пишет n замеров вдоль короткого «маршрута» — по одному в секунду.

    Секундный шаг важен: коридор маски строится только между детекциями,
    отстоящими не дальше bridge_max_s (по умолчанию 30 с).
    """
    for i in range(n):
        insert_observation(conn, {
            "bssid": bssid,
            "timestamp": "{}T10:{:02d}:{:02d}Z".format(day, i // 60, i % 60),
            # ~1.1 м на шаг по широте — точки попадают в один кластер
            "lat": lat0 + i * 0.00001,
            "lon": lon0,
            "rssi": rssi0 - (i % 7),
            "channel": 6, "frequency": 2437.0,
        })


@pytest.fixture
def seeded(conn):
    """Две сети: NetA — 40 замеров в день1, NetB — 30 замеров в день2."""
    upsert_network(conn, NET_A)
    upsert_network(conn, NET_B)
    _track(conn, NET_A["bssid"], DAY1, 40)
    _track(conn, NET_B["bssid"], DAY2, 30, lat0=55.7100, lon0=37.6100, rssi0=-70)
    conn.commit()
    return conn


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Базовый путь: БД → HTML
# ---------------------------------------------------------------------------

def test_map_builds_html_with_layers(seeded, tmp_path):
    out = str(tmp_path / "map.html")
    result = exporter.export_map_html(seeded, out)

    assert result["networks"] == 2          # обе сети прошли min_points=20
    assert result["points"] == 70
    assert result["bytes"] > 0
    assert os.path.exists(out)

    html = _read(out)
    assert "<html>" in html
    assert "</html>" in html                # файл дописан целиком
    assert "data:image/png;base64," in html  # растровые оверлеи вшиты в файл
    assert "NetA" in html and "NetB" in html  # имена слоёв в переключателе


def test_map_output_is_self_contained_no_local_refs(seeded, tmp_path):
    # Карта должна открываться как один файл: локальных ссылок на соседние файлы быть не должно
    out = str(tmp_path / "map.html")
    exporter.export_map_html(seeded, out)
    html = _read(out)
    assert "file://" not in html
    assert os.path.basename(str(tmp_path)) not in html


# ---------------------------------------------------------------------------
# Фильтры: сеть и период
# ---------------------------------------------------------------------------

def test_map_bssid_filter_builds_single_layer(seeded, tmp_path):
    out = str(tmp_path / "one.html")
    result = exporter.export_map_html(seeded, out, bssid_filter=NET_A["bssid"])

    assert result["networks"] == 1
    html = _read(out)
    assert "NetA" in html
    assert "NetB" not in html


def test_map_since_until_narrows_to_one_day(seeded, tmp_path):
    since = exporter.normalize_time_bound(DAY2)
    until = exporter.normalize_time_bound(DAY2, end_of_day=True)
    out = str(tmp_path / "day2.html")

    result = exporter.export_map_html(seeded, out, since=since, until=until)

    assert result["networks"] == 1          # NetA (день1) вне окна
    assert result["points"] == 30
    html = _read(out)
    assert "NetB" in html
    assert "NetA" not in html


def test_map_period_in_title(seeded, tmp_path):
    since = exporter.normalize_time_bound(DAY2)
    out = str(tmp_path / "titled.html")
    exporter.export_map_html(seeded, out, since=since)
    assert "период" in _read(out)


# ---------------------------------------------------------------------------
# Понятные ошибки вместо трейсбеков
# ---------------------------------------------------------------------------

def test_map_empty_db_raises_map_export_error(conn, tmp_path):
    with pytest.raises(exporter.MapExportError):
        exporter.export_map_html(conn, str(tmp_path / "empty.html"))


def test_map_empty_period_raises_map_export_error(seeded, tmp_path):
    with pytest.raises(exporter.MapExportError) as exc:
        exporter.export_map_html(
            seeded, str(tmp_path / "none.html"),
            since="2000-01-01T00:00:00Z", until="2000-01-02T23:59:59Z",
        )
    assert "фильтр" in str(exc.value).lower()


def test_map_too_strict_min_points_raises_map_export_error(seeded, tmp_path):
    with pytest.raises(exporter.MapExportError) as exc:
        exporter.export_map_html(
            seeded, str(tmp_path / "none.html"), map_params={"min_points": 100_000},
        )
    assert "min_points" in str(exc.value)


def test_map_observations_without_gps_are_ignored(conn, tmp_path):
    # Наблюдения без координат в карту не годятся — должна быть внятная ошибка
    upsert_network(conn, NET_A)
    for i in range(30):
        insert_observation(conn, {
            "bssid": NET_A["bssid"], "timestamp": "{}T10:00:{:02d}Z".format(DAY1, i),
            "rssi": -60, "channel": 6, "frequency": 2437.0,   # lat/lon отсутствуют
        })
    conn.commit()

    with pytest.raises(exporter.MapExportError):
        exporter.export_map_html(conn, str(tmp_path / "nogps.html"))


# ---------------------------------------------------------------------------
# Настройки из секции map
# ---------------------------------------------------------------------------

def test_map_params_top_limits_layers(seeded, tmp_path):
    out = str(tmp_path / "top1.html")
    result = exporter.export_map_html(seeded, out, map_params={"top": 1})
    assert result["networks"] == 1


def test_map_params_show_points_adds_points_layer(seeded, tmp_path):
    out_off = str(tmp_path / "off.html")
    out_on = str(tmp_path / "on.html")
    exporter.export_map_html(seeded, out_off, map_params={"show_points": False})
    exporter.export_map_html(seeded, out_on, map_params={"show_points": True})

    # Проверяем сами маркеры, а не подпись слоя: folium экранирует кириллицу
    # в JS-именах слоёв (точки), искать её как текст нельзя.
    assert "circleMarker" not in _read(out_off)
    assert _read(out_on).count("circleMarker") == 70   # все замеры обеих сетей


def test_map_params_partial_override_keeps_other_defaults(seeded, tmp_path):
    # Передаём только один параметр — остальные должны браться из _MAP_DEFAULTS
    out = str(tmp_path / "partial.html")
    result = exporter.export_map_html(seeded, out, map_params={"grid_step_m": 5.0})
    assert result["networks"] == 2           # min_points=20 по умолчанию не потерялся


def test_map_defaults_mirror_config_section():
    # Дефолты экспортёра и конфига должны совпадать, иначе автономный запуск
    # (без settings.yaml) вёл бы себя иначе, чем через оркестратор.
    from src.config import _DEFAULTS
    assert exporter._MAP_DEFAULTS == _DEFAULTS["map"]


# ---------------------------------------------------------------------------
# Подключение к оркестратору (do_exports)
# ---------------------------------------------------------------------------

def _db_file_copy(connection, dest_path):
    """do_exports открывает БД сама по пути — кладём наполненную базу в отдельный файл.

    Через backup API, а не копированием файла: база открыта в WAL-режиме, и часть
    данных лежит в соседнем ``-wal``, который простой copy не захватил бы.
    """
    import sqlite3
    connection.commit()
    dest = sqlite3.connect(dest_path)
    try:
        connection.backup(dest)
    finally:
        dest.close()
    return dest_path


def test_do_exports_map_writes_html_and_summary(seeded, tmp_path, monkeypatch):
    db_path = _db_file_copy(seeded, str(tmp_path / "wifi.db"))
    monkeypatch.setattr(cli, "EXPORT_DIR", str(tmp_path / "exp"))

    cli.do_exports(db_path, ["map"], bssid_filter=None, ssid_filter=None)

    files = os.listdir(str(tmp_path / "exp"))
    html_files = [f for f in files if f.startswith("map_") and f.endswith(".html")]
    assert len(html_files) == 1


def test_do_exports_map_failure_does_not_break_other_profiles(tmp_path, conn, monkeypatch, caplog):
    # Пустая база: map не построится, но ap_status должен отработать штатно
    db_path = _db_file_copy(conn, str(tmp_path / "empty.db"))
    monkeypatch.setattr(cli, "EXPORT_DIR", str(tmp_path / "exp2"))

    cli.do_exports(db_path, ["map", "ap_status"], bssid_filter=None, ssid_filter=None)

    files = os.listdir(str(tmp_path / "exp2"))
    assert not any(f.startswith("map_") for f in files)      # карта не построена
    assert any(f.startswith("ap_status_") for f in files)    # соседний профиль отработал
    assert "не выполнен" in caplog.text                      # причина показана без трейсбека
