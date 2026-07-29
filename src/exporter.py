# -*- coding: utf-8 -*-
"""
src/exporter.py

Экспортирует данные из SQLite базы в форматы раздела 13 ТЗ:
    heatmap           CSV   — тепловая карта (все сети вперемешку, опц. фильтр по ssid/bssid)
    heatmap_networks  CSV×N — тепловая карта ПО КАЖДОЙ СЕТИ (один файл на BSSID + манифест)
    full              GPKG + CSV — полный датасет (оба файла всегда)
    wigle             CSV   — формат WigleWifi-1.4 (для сверки/загрузки на wigle.net)
    ap_status         CSV   — результаты проверки точек оператора

Запускается напрямую: python -m src.exporter --profile <профиль>
"""

import argparse
import csv
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

from pygeopkg.conversion.to_geopkg_geom import make_gpkg_geom_header, point_to_gpkg_point
from pygeopkg.core.field import Field
from pygeopkg.core.geopkg import GeoPackage
from pygeopkg.core.srs import SRS
from pygeopkg.shared.constants import SHAPE
from pygeopkg.shared.enumeration import GeometryType, GPKGFLavors, SQLFieldTypes

logger = logging.getLogger(__name__)

_WGS84_SRS_ID = 4326
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _ts():
    """Текущее время в формате YYYYMMDD_HHMMSS для имён файлов."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_dir(path):
    """Создаёт папку для файла если не существует."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)


def _write_csv(path, header, rows):
    """
    Записывает CSV файл с заголовком и строками данных.
    Кодировка utf-8-sig — совместима с QGIS и Excel.
    Возвращает количество записанных строк данных.
    """
    _ensure_dir(path)
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, dialect="excel")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _sanitize_filename(text):
    """Заменяет символы, небезопасные для имени файла, на '_'. Пусто → 'hidden'."""
    text = (text or "").strip()
    if not text:
        return "hidden"
    return _UNSAFE_FILENAME_RE.sub("_", text)


def _wigle_time(iso_ts):
    """Конвертирует наш ISO8601 (``...THH:MM:SSZ``) в формат WigleWifi-1.4.

    Спецификация (api.wigle.net/csvFormat-1_4.html): ``FirstSeen`` — секундная
    точность вида ``YYYY-MM-DD HH:MM:SS`` (пробел вместо ``T``, без ``Z``).

    Args:
        iso_ts: Строка ISO8601 UTC или ``None``/пусто.

    Returns:
        Строка нужного формата; при нераспознанном формате — исходная строка
        как есть (не роняем экспорт из-за одного поля); пусто → ``""``.
    """
    if not iso_ts:
        return ""
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_ts


def normalize_time_bound(value, end_of_day=False):
    """Приводит пользовательское значение даты/времени к нашему ISO8601 UTC.

    Используется флагами ``--since``/``--until`` (в cli.py — ``--export-since``/
    ``--export-until``) для выборки записей за конкретный период — например,
    когда в одну БД пишется много данных и нужно выделить из неё один день.

    Принимает:
      - ``YYYY-MM-DD`` — только дата. Для нижней границы (``end_of_day=False``)
        подставляется начало дня ``00:00:00``, для верхней (``end_of_day=True``)
        — конец дня ``23:59:59``. Поэтому ``--since 2026-07-23 --until
        2026-07-23`` захватывает ВЕСЬ этот день целиком (обе границы включительно).
      - ``YYYY-MM-DDTHH:MM:SS`` или ``YYYY-MM-DDTHH:MM:SSZ`` — точный момент,
        используется как есть, без подстановки.

    Момент считается заданным в UTC — как и все временные метки в проекте
    (``observations.timestamp`` / ``ap_health.timestamp`` — строки вида
    ``YYYY-MM-DDTHH:MM:SSZ``, сравнимые лексикографически).

    Args:
        value:      Строка от пользователя или ``None``.
        end_of_day: Если задана только дата — взять конец дня (23:59:59)
                    вместо начала (00:00:00). На полный datetime не влияет.

    Returns:
        Строка ``YYYY-MM-DDTHH:MM:SSZ`` или ``None`` (если ``value`` пусто/None).

    Raises:
        ValueError: если формат не распознан ни как дата, ни как datetime.
    """
    if not value:
        return None
    text = value.strip()

    try:
        d = datetime.strptime(text, "%Y-%m-%d")
        time_part = "23:59:59" if end_of_day else "00:00:00"
        return "{}T{}Z".format(d.strftime("%Y-%m-%d"), time_part)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    raise ValueError(
        "Не удалось разобрать дату/время {!r} — используйте YYYY-MM-DD или "
        "YYYY-MM-DDTHH:MM:SS".format(value)
    )


def _wigle_authmode(encryption):
    """Строит поле ``AuthMode`` в скобочной нотации WigleWifi-1.4.

    Спецификация: ``"[WPA2-EAP-CCMP][ESS]"`` — тип шифрования в скобках,
    затем ``[ESS]`` (обычная инфраструктурная сеть). Открытые сети — только
    ``[ESS]`` (без скобки шифрования).

    Args:
        encryption: Строка из ``networks.encryption`` (напр. ``"WPA2-PSK"``,
            ``"Open"``, ``None``).

    Returns:
        Строка AuthMode вида ``"[WPA2-PSK][ESS]"`` или ``"[ESS]"``.
    """
    enc = (encryption or "").strip().upper()
    if not enc or enc in ("OPEN", "NONE"):
        return "[ESS]"
    return "[{}][ESS]".format(enc)


# ---------------------------------------------------------------------------
# Профили экспорта
# ---------------------------------------------------------------------------

def resolve_bssid_by_ssid(conn, ssid):
    """Ищет BSSID сети по имени (SSID) — для интерфейса, где известно только имя.

    Оператор (и вообще любой пользователь CLI) не обязан знать MAC конкретной
    сети — обычно ему известно только её имя. Однако публичные/дефолтные SSID
    (напр. «Keenetic-1234») часто повторяются у РАЗНЫХ физических точек в
    разных местах — по одному имени их не различить. В этом случае функция
    возвращает список кандидатов вместо угадывания: разрешать коллизию нужно
    по MAC (``--export-bssid``/``--bssid``).

    Args:
        conn: Соединение с БД.
        ssid: Точное имя сети (регистрозависимо, как и сам Wi-Fi SSID).

    Returns:
        Кортеж ``(bssid, candidates)``:
          - Ровно одно совпадение — ``(bssid, [bssid])``.
          - Совпадений нет — ``(None, [])``.
          - Несколько совпадений (коллизия имени) — ``(None, [bssid, ...])``.
    """
    rows = conn.execute(
        "SELECT bssid FROM networks WHERE ssid = ? ORDER BY bssid", (ssid,)
    ).fetchall()
    candidates = [r["bssid"] for r in rows]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def export_heatmap_csv(conn, output_path, bssid_filter=None, since=None, until=None):
    """
    Экспорт для тепловых карт в QGIS.
    Только наблюдения с GPS-координатами (has_gps = 1).
    Опционально сужается по конкретной сети (``bssid_filter``) и/или периоду
    времени (``since``/``until`` — ISO8601 UTC, обе границы включительно;
    см. :func:`normalize_time_bound`) — полезно, когда в одну БД пишется много
    данных за разные заезды и нужно выделить, например, один день.
    Возвращает количество строк.
    """
    where = ["o.has_gps = 1"]
    params = []
    if bssid_filter:
        where.append("o.bssid = ?")
        params.append(bssid_filter)
    if since:
        where.append("o.timestamp >= ?")
        params.append(since)
    if until:
        where.append("o.timestamp <= ?")
        params.append(until)

    sql = """
        SELECT o.lat, o.lon, o.rssi, o.bssid, n.ssid, o.timestamp
        FROM observations o
        JOIN networks n ON o.bssid = n.bssid
        WHERE {}
        ORDER BY o.timestamp
    """.format(" AND ".join(where))
    rows = conn.execute(sql, params).fetchall()

    if bssid_filter:
        logger.info("Heatmap: фильтр по BSSID %s", bssid_filter)
    if since or until:
        logger.info("Heatmap: диапазон времени %s .. %s", since or "-inf", until or "+inf")

    header = ["lat", "lon", "rssi", "bssid", "ssid", "timestamp"]
    count = _write_csv(output_path, header, rows)

    if count == 0:
        logger.warning(
            "Heatmap: нет данных с GPS-координатами%s",
            " для BSSID {}".format(bssid_filter) if bssid_filter else "",
        )
    else:
        logger.info("Heatmap: экспортировано %d строк → %s", count, output_path)
    return count


def export_heatmap_per_network(conn, out_dir, since=None, until=None):
    """Тепловая карта ПО КАЖДОЙ СЕТИ: один CSV на BSSID + манифест.

    Каждая сеть уже имеет множество сэмплов ``(lat, lon, rssi)`` в разных
    точках маршрута (см. kismet_runner: packets → observations). Раньше их
    приходилось разбирать по одному BSSID вручную (``--export-bssid``); этот
    профиль делает то же самое сразу для ВСЕХ сетей за один вызов — каждый
    файл сразу готов к интерполяции (IDW) в QGIS без дополнительной фильтрации
    (раздел 14 ТЗ).

    ``since``/``until`` (см. :func:`normalize_time_bound`) сужают и отбор
    сетей, и содержимое каждого файла до заданного периода — полезно, когда
    в одну БД пишется много данных за разные заезды и нужно выделить,
    например, один день.

    Args:
        conn:    Соединение с БД.
        out_dir: Каталог для файлов сетей + ``_manifest.csv``.
        since:   Нижняя граница времени наблюдений (ISO8601 UTC), включительно.
        until:   Верхняя граница времени наблюдений (ISO8601 UTC), включительно.

    Returns:
        ``{"networks": <число сетей>, "samples": <суммарное число сэмплов>}``.
    """
    where = ["has_gps = 1"]
    params = []
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    if until:
        where.append("timestamp <= ?")
        params.append(until)

    bssids = conn.execute(
        "SELECT DISTINCT bssid FROM observations WHERE {}".format(" AND ".join(where)),
        params,
    ).fetchall()

    os.makedirs(out_dir, exist_ok=True)
    manifest_rows = []
    total_samples = 0

    for row in bssids:
        bssid = row["bssid"]
        ssid_row = conn.execute(
            "SELECT ssid FROM networks WHERE bssid = ?", (bssid,)
        ).fetchone()
        ssid = ssid_row["ssid"] if ssid_row else None

        filename = "{}_{}.csv".format(
            _sanitize_filename(ssid), _sanitize_filename(bssid)
        )
        out_path = os.path.join(out_dir, filename)
        count = export_heatmap_csv(conn, out_path, bssid_filter=bssid, since=since, until=until)

        manifest_rows.append([bssid, ssid or "", count, filename])
        total_samples += count

    manifest_path = os.path.join(out_dir, "_manifest.csv")
    _write_csv(manifest_path, ["bssid", "ssid", "n_samples", "file"], manifest_rows)

    if not bssids:
        logger.warning("Heatmap по сетям: нет данных с GPS-координатами")
    else:
        logger.info(
            "Heatmap по сетям: %d сетей, %d сэмплов → %s",
            len(bssids), total_samples, out_dir,
        )
    return {"networks": len(bssids), "samples": total_samples}


def export_full_dataset_csv(conn, output_path, since=None, until=None):
    """
    Полный датасет: все поля networks + observations (включая записи без GPS).
    ``since``/``until`` (см. :func:`normalize_time_bound`) сужают выборку по
    ``observations.timestamp`` — полезно, когда в одну БД пишется много
    данных и нужно выделить, например, один день.
    Возвращает количество строк.
    """
    where = []
    params = []
    if since:
        where.append("o.timestamp >= ?")
        params.append(since)
    if until:
        where.append("o.timestamp <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = """
        SELECT n.bssid, n.ssid, n.encryption, n.manufacturer,
               n.channel, n.frequency, n.first_seen, n.last_seen,
               o.id        AS obs_id,
               o.timestamp,
               o.lat,
               o.lon,
               o.rssi,
               o.channel   AS obs_channel,
               o.has_gps
        FROM observations o
        JOIN networks n ON o.bssid = n.bssid
        {}
        ORDER BY o.timestamp
    """.format(where_sql)
    rows = conn.execute(sql, params).fetchall()
    header = [
        "bssid", "ssid", "encryption", "manufacturer",
        "channel", "frequency", "first_seen", "last_seen",
        "obs_id", "timestamp", "lat", "lon",
        "rssi", "obs_channel", "has_gps",
    ]
    count = _write_csv(output_path, header, rows)

    if count == 0:
        logger.warning("Full dataset (CSV): нет данных в базе")
    else:
        logger.info("Full dataset (CSV): экспортировано %d строк → %s", count, output_path)
    return count


def export_full_dataset_gpkg(conn, output_path, since=None, until=None):
    """Полный датасет в формате GeoPackage (.gpkg) — пространственный слой точек.

    Раздел 13 ТЗ требует GeoPackage для полного датасета (готовый слой для QGIS,
    без ручного «Add Delimited Text»). Пишется через ``pygeopkg`` (pure-python,
    без GDAL) — уместно для автономной установки на Kali/OVA.

    Включаются только наблюдения с координатами (``has_gps=1``) — геометрия
    точки без координат не имеет смысла; полная таблица (в т.ч. записи без
    GPS) доступна в CSV-варианте (:func:`export_full_dataset_csv`).
    ``since``/``until`` (см. :func:`normalize_time_bound`) сужают выборку по
    ``observations.timestamp``.

    Args:
        conn:        Соединение с БД.
        output_path: Путь к создаваемому ``.gpkg`` (перезаписывается, если существует).
        since:       Нижняя граница времени наблюдений (ISO8601 UTC), включительно.
        until:       Верхняя граница времени наблюдений (ISO8601 UTC), включительно.

    Returns:
        Количество вставленных точек.
    """
    where = ["o.has_gps = 1"]
    params = []
    if since:
        where.append("o.timestamp >= ?")
        params.append(since)
    if until:
        where.append("o.timestamp <= ?")
        params.append(until)

    sql = """
        SELECT n.bssid, n.ssid, n.encryption, n.manufacturer,
               n.channel, n.frequency, n.first_seen, n.last_seen,
               o.id        AS obs_id,
               o.timestamp,
               o.lat, o.lon,
               o.rssi,
               o.channel   AS obs_channel,
               o.has_gps
        FROM observations o
        JOIN networks n ON o.bssid = n.bssid
        WHERE {}
        ORDER BY o.timestamp
    """.format(" AND ".join(where))
    rows = conn.execute(sql, params).fetchall()

    # Абсолютный путь обязателен: pygeopkg.GeoPackage.create() сам проверяет
    # dirname(target_path) — для голого имени файла без каталога (напр. просто
    # "full.gpkg") dirname() вернёт '', а exists('') на Windows/Kali == False,
    # и создание упадёт с "Containing folder of target location does not exist"
    # даже если каталог (cwd) на самом деле существует.
    output_path = os.path.abspath(output_path)

    _ensure_dir(output_path)
    # GeoPackage.create() и сам удаляет существующий файл, но делаем это явно —
    # понятнее по логам и не зависит от версии pygeopkg.
    if os.path.exists(output_path):
        os.remove(output_path)

    gpkg = GeoPackage.create(output_path, flavor=GPKGFLavors.epsg)
    # 4326 уже определён (полный WKT) в create(flavor=epsg) — свой definition не нужен.
    srs = SRS("WGS_84", "EPSG", _WGS84_SRS_ID, "")

    fields = (
        Field("bssid", SQLFieldTypes.text),
        Field("ssid", SQLFieldTypes.text),
        Field("encryption", SQLFieldTypes.text),
        Field("manufacturer", SQLFieldTypes.text),
        Field("channel", SQLFieldTypes.integer),
        Field("frequency", SQLFieldTypes.real),
        Field("first_seen", SQLFieldTypes.text),
        Field("last_seen", SQLFieldTypes.text),
        Field("obs_id", SQLFieldTypes.integer),
        Field("timestamp", SQLFieldTypes.text),
        Field("rssi", SQLFieldTypes.integer),
        Field("obs_channel", SQLFieldTypes.integer),
        Field("has_gps", SQLFieldTypes.integer),
    )
    fc = gpkg.create_feature_class(
        "wifi_observations", srs, fields=fields, shape_type=GeometryType.point
    )

    hdr = make_gpkg_geom_header(_WGS84_SRS_ID)
    field_names = [SHAPE] + [f.name for f in fields]
    data = []
    for r in rows:
        wkb = point_to_gpkg_point(hdr, r["lon"], r["lat"])   # x=lon, y=lat
        data.append((
            wkb, r["bssid"], r["ssid"], r["encryption"], r["manufacturer"],
            r["channel"], r["frequency"], r["first_seen"], r["last_seen"],
            r["obs_id"], r["timestamp"], r["rssi"], r["obs_channel"], r["has_gps"],
        ))

    if data:
        fc.insert_rows(field_names, data)

    count = len(data)
    if count == 0:
        logger.warning("Full dataset (GPKG): нет данных с GPS-координатами")
    else:
        logger.info("Full dataset (GPKG): экспортировано %d точек → %s", count, output_path)
    return count


def export_full_dataset(conn, out_base, since=None, until=None):
    """Полный датасет — ОБА формата сразу (раздел 13 ТЗ: GeoPackage (.gpkg) / CSV).

    Args:
        conn:     Соединение с БД.
        out_base: Путь БЕЗ расширения; создаются ``out_base + ".gpkg"`` и
                  ``out_base + ".csv"``.
        since:    Нижняя граница времени наблюдений (ISO8601 UTC), включительно.
        until:    Верхняя граница времени наблюдений (ISO8601 UTC), включительно.

    Returns:
        ``{"gpkg": <точек>, "csv": <строк>}``.
    """
    gpkg_count = export_full_dataset_gpkg(conn, out_base + ".gpkg", since=since, until=until)
    csv_count = export_full_dataset_csv(conn, out_base + ".csv", since=since, until=until)
    return {"gpkg": gpkg_count, "csv": csv_count}


def export_wigle_csv(conn, output_path, since=None, until=None):
    """
    Экспорт в формате WigleWifi-1.4 (сверено с api.wigle.net/csvFormat-1_4.html).
    Одна строка на сеть (первое наблюдение с GPS, при заданном диапазоне —
    первое наблюдение ИЗ этого диапазона; см. :func:`normalize_time_bound`).
    Сеть, не наблюдавшаяся ни разу в заданном диапазоне, из выгрузки исключается
    (без диапазона поведение как раньше — попадают все сети, LEFT JOIN).
    Возвращает количество строк данных (без заголовочных строк Wigle).
    """
    time_where = []
    params = []
    if since:
        time_where.append("timestamp >= ?")
        params.append(since)
    if until:
        time_where.append("timestamp <= ?")
        params.append(until)
    time_sql = (" AND " + " AND ".join(time_where)) if time_where else ""
    # Диапазон задан → JOIN (сеть без наблюдения в окне не попадает в выгрузку);
    # без диапазона — LEFT JOIN, как раньше (попадают все сети из networks).
    join_kind = "JOIN" if time_where else "LEFT JOIN"

    # Первое наблюдение с GPS для каждой сети (в пределах диапазона, если задан)
    sql = """
        SELECT n.bssid, n.ssid, n.encryption, n.first_seen,
            o.channel, o.rssi, o.lat, o.lon
        FROM networks n
        {join_kind} observations o ON o.id = (
            SELECT id
            FROM observations
            WHERE bssid = n.bssid AND has_gps = 1{time_sql}
            ORDER BY id ASC
            LIMIT 1
        )
    """.format(join_kind=join_kind, time_sql=time_sql)
    rows = conn.execute(sql, params).fetchall()

    _ensure_dir(output_path)
    count = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, dialect="excel")

        # Строка 1: заголовок формата Wigle
        writer.writerow([
            "WigleWifi-1.4",
            "appRelease=wifi-monitor",
            "model=custom",
            "release=1.0",
            "device=wardrive",
            "display=wifi-monitor",
            "board=custom",
            "brand=custom",
        ])

        # Строка 2: названия колонок Wigle
        writer.writerow([
            "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
            "CurrentLatitude", "CurrentLongitude",
            "AltitudeMeters", "AccuracyMeters", "Type",
        ])

        # Данные
        for row in rows:
            bssid, ssid, encryption, first_seen, channel, rssi, lat, lon = row
            writer.writerow([
                bssid or "",
                ssid or "",
                _wigle_authmode(encryption),
                _wigle_time(first_seen),
                channel or "",
                rssi or "",
                lat if lat is not None else "",
                lon if lon is not None else "",
                0,      # AltitudeMeters
                0,      # AccuracyMeters
                "WIFI",
            ])
            count += 1

    if count == 0:
        logger.warning("Wigle: нет данных для экспорта")
    else:
        logger.info("Wigle: экспортировано %d строк → %s", count, output_path)
    return count


def export_ap_status_csv(conn, output_path, since=None, until=None):
    """
    Экспорт статусов проверок точек оператора.
    Только поля из ap_health — никаких паролей и конфигурации.
    ``since``/``until`` (см. :func:`normalize_time_bound`) сужают выборку по
    ``ap_health.timestamp``.
    Возвращает количество строк.
    """
    where = []
    params = []
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    if until:
        where.append("timestamp <= ?")
        params.append(until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = """
        SELECT ap_id, timestamp, status, rtt_ms, lat, lon
        FROM ap_health
        {}
        ORDER BY timestamp
    """.format(where_sql)
    rows = conn.execute(sql, params).fetchall()
    header = ["ap_id", "timestamp", "status", "rtt", "lat", "lon"]
    count = _write_csv(output_path, header, rows)

    if count == 0:
        logger.warning("AP status: нет данных о проверках точек")
    else:
        logger.info("AP status: экспортировано %d строк → %s", count, output_path)
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Профили с одним выходным файлом (для них argparse-обвязка одинакова)
_SINGLE_FILE_PROFILES = {
    "heatmap":   export_heatmap_csv,
    "wigle":     export_wigle_csv,
    "ap_status": export_ap_status_csv,
}
# Все профили — для справки/валидации choices (full и heatmap_networks обрабатываются особо)
EXPORT_PROFILES = tuple(_SINGLE_FILE_PROFILES) + ("full", "heatmap_networks")

if __name__ == "__main__":
    import sys

    # Пробуем подгрузить конфиг проекта — если модуль доступен
    try:
        from src.config import load_config
        _config = load_config()
        _default_db = _config.get("db_path", "data/wifi_monitor.db")
    except Exception:
        _default_db = "data/wifi_monitor.db"

    parser = argparse.ArgumentParser(
        prog="python -m src.exporter",
        description="Экспорт данных wifi-monitor в форматы раздела 13 ТЗ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
профили:
  heatmap           Тепловая карта в QGIS: все сети в одном CSV (опц. --ssid/--bssid)
  heatmap_networks  Тепловая карта ПО КАЖДОЙ СЕТИ: CSV на сеть + манифест (каталог)
  full              Полный датасет: GeoPackage (.gpkg) + CSV — создаются ОБА файла
  wigle             Формат WigleWifi-1.4 для сверки/загрузки на wigle.net
  ap_status         Результаты проверки точек оператора

примеры:
  python -m src.exporter --profile heatmap
  python -m src.exporter --profile heatmap --ssid "MyNet_Garage"
  python -m src.exporter --profile heatmap --bssid AA:BB:CC:DD:EE:FF
  python -m src.exporter --profile heatmap_networks
  python -m src.exporter --profile full --db data/wifi_monitor.db
  python -m src.exporter --profile full --since 2026-07-23 --until 2026-07-23
  python -m src.exporter --profile wigle
  python -m src.exporter --profile ap_status --out /tmp/report.csv
        """,
    )
    parser.add_argument(
        "--profile", required=True, choices=EXPORT_PROFILES,
        metavar="<профиль>",
        help=" | ".join(EXPORT_PROFILES),
    )
    bssid_group = parser.add_mutually_exclusive_group()
    bssid_group.add_argument(
        "--ssid",
        metavar="<имя сети>",
        help="Фильтр по имени сети (только для профиля heatmap). Обычный способ "
             "выбрать сеть — MAC знать не нужно; если несколько разных точек "
             "вещают одно и то же имя, попросит уточнить через --bssid",
    )
    bssid_group.add_argument(
        "--bssid",
        metavar="AA:BB:CC:DD:EE:FF",
        help="Фильтр по MAC точки (только для профиля heatmap) — нужен только "
             "для разрешения коллизии одинаковых имён сетей",
    )
    parser.add_argument(
        "--db", default=_default_db,
        metavar="<путь>",
        help="Путь к SQLite базе (default: {})".format(_default_db),
    )
    parser.add_argument(
        "--out",
        metavar="<путь>",
        help="Путь к выходу: файл для одиночных профилей, БЕЗ расширения для "
             "full (.gpkg/.csv добавятся сами), каталог для heatmap_networks "
             "(default: export/<профиль>_<timestamp>[.csv])",
    )
    parser.add_argument(
        "--since", metavar="<YYYY-MM-DD[THH:MM:SS]>",
        help="нижняя граница времени наблюдений, включительно, UTC (для всех "
             "профилей). Только дата = начало дня",
    )
    parser.add_argument(
        "--until", metavar="<YYYY-MM-DD[THH:MM:SS]>",
        help="верхняя граница времени наблюдений, включительно, UTC. Только дата "
             "= конец дня — поэтому --since 2026-07-23 --until 2026-07-23 "
             "выберет весь этот день целиком",
    )

    args = parser.parse_args()

    # Валидация: --ssid/--bssid только для heatmap
    if (args.ssid or args.bssid) and args.profile != "heatmap":
        parser.error("--ssid/--bssid можно использовать только с --profile heatmap")

    try:
        since = normalize_time_bound(args.since, end_of_day=False)
        until = normalize_time_bound(args.until, end_of_day=True)
    except ValueError as exc:
        parser.error(str(exc))

    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    # Открываем БД
    if not os.path.exists(args.db):
        print("Ошибка: база данных не найдена: {}".format(args.db))
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # --ssid: обычный способ выбрать сеть без знания MAC. Разрешаем в BSSID
    # здесь же, до вызова экспорта, чтобы явно сообщить о коллизии имён.
    bssid_filter = args.bssid
    if args.ssid:
        bssid_filter, candidates = resolve_bssid_by_ssid(conn, args.ssid)
        if bssid_filter is None:
            conn.close()
            if not candidates:
                print("Сеть с именем {!r} не найдена.".format(args.ssid))
            else:
                print(
                    "Имя {!r} совпадает с {} разными точками (разные MAC) — "
                    "уточните через --bssid:\n  {}".format(
                        args.ssid, len(candidates), "\n  ".join(candidates),
                    )
                )
            sys.exit(1)

    try:
        if args.profile == "full":
            out_base = args.out or "export/full_{}".format(_ts())
            result = export_full_dataset(conn, out_base, since=since, until=until)
            print("Экспортировано: {} точек → {}.gpkg, {} строк → {}.csv".format(
                result["gpkg"], out_base, result["csv"], out_base,
            ))
        elif args.profile == "heatmap_networks":
            out_dir = args.out or "export/heatmap_networks_{}".format(_ts())
            result = export_heatmap_per_network(conn, out_dir, since=since, until=until)
            print("Экспортировано: {} сетей, {} сэмплов → {}/".format(
                result["networks"], result["samples"], out_dir,
            ))
        else:
            fn = _SINGLE_FILE_PROFILES[args.profile]
            out_path = args.out or "export/{}_{}.csv".format(args.profile, _ts())

            # heatmap принимает дополнительный аргумент bssid_filter (уже
            # разрешённый из --ssid выше либо взятый напрямую из --bssid)
            if args.profile == "heatmap":
                count = fn(conn, out_path, bssid_filter=bssid_filter, since=since, until=until)
            else:
                count = fn(conn, out_path, since=since, until=until)

            print("Экспортировано {} строк → {}".format(count, out_path))

    except sqlite3.Error as exc:
        print("Ошибка базы данных: {}".format(exc))
        sys.exit(1)
    except OSError as exc:
        print("Ошибка записи файла: {}".format(exc))
        sys.exit(1)
    finally:
        conn.close()
