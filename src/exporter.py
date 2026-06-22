# -*- coding: utf-8 -*-
"""
src/exporter.py

Экспортирует данные из SQLite базы в CSV-файлы разных форматов.
Запускается напрямую: python -m src.exporter --profile <профиль>
"""

import argparse
import csv
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Профили экспорта
# ---------------------------------------------------------------------------

def export_heatmap_csv(conn, output_path, bssid_filter=None):
    """
    Экспорт для тепловых карт в QGIS.
    Только наблюдения с GPS-координатами (has_gps = 1).
    Возвращает количество строк.
    """
    if bssid_filter:
        sql = """
            SELECT o.lat, o.lon, o.rssi, o.bssid, n.ssid, o.timestamp
            FROM observations o
            JOIN networks n ON o.bssid = n.bssid
            WHERE o.has_gps = 1
              AND o.bssid = ?
            ORDER BY o.timestamp
        """
        rows = conn.execute(sql, (bssid_filter,)).fetchall()
        logger.info("Heatmap: фильтр по BSSID %s", bssid_filter)
    else:
        sql = """
            SELECT o.lat, o.lon, o.rssi, o.bssid, n.ssid, o.timestamp
            FROM observations o
            JOIN networks n ON o.bssid = n.bssid
            WHERE o.has_gps = 1
            ORDER BY o.timestamp
        """
        rows = conn.execute(sql).fetchall()

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


def export_full_dataset_csv(conn, output_path):
    """
    Полный датасет: все поля networks + observations.
    Возвращает количество строк.
    """
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
        ORDER BY o.timestamp
    """
    rows = conn.execute(sql).fetchall()
    header = [
        "bssid", "ssid", "encryption", "manufacturer",
        "channel", "frequency", "first_seen", "last_seen",
        "obs_id", "timestamp", "lat", "lon",
        "rssi", "obs_channel", "has_gps",
    ]
    count = _write_csv(output_path, header, rows)

    if count == 0:
        logger.warning("Full dataset: нет данных в базе")
    else:
        logger.info("Full dataset: экспортировано %d строк → %s", count, output_path)
    return count


def export_wigle_csv(conn, output_path):
    """
    Экспорт в формате WigleWifi-1.4.
    Одна строка на сеть (первое наблюдение с GPS).
    Возвращает количество строк данных (без заголовочных строк Wigle).
    """
    # Первое наблюдение с GPS для каждой сети
    sql = """
        SELECT n.bssid, n.ssid, n.encryption, n.first_seen,
            o.channel, o.rssi, o.lat, o.lon
        FROM networks n
        LEFT JOIN observations o ON o.id = (
            SELECT id 
            FROM observations 
            WHERE bssid = n.bssid AND has_gps = 1 
            ORDER BY id ASC 
            LIMIT 1
        )
    """
    rows = conn.execute(sql).fetchall()

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
                encryption or "",
                first_seen or "",
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


def export_ap_status_csv(conn, output_path):
    """
    Экспорт статусов проверок точек оператора.
    Только поля из ap_health — никаких паролей и конфигурации.
    Возвращает количество строк.
    """
    sql = """
        SELECT ap_id, timestamp, status, rtt_ms, lat, lon
        FROM ap_health
        ORDER BY timestamp
    """
    rows = conn.execute(sql).fetchall()
    header = ["ap_id", "timestamp", "status", "rtt_ms", "lat", "lon"]
    count = _write_csv(output_path, header, rows)

    if count == 0:
        logger.warning("AP status: нет данных о проверках точек")
    else:
        logger.info("AP status: экспортировано %d строк → %s", count, output_path)
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_PROFILES = {
    "heatmap":   export_heatmap_csv,
    "full":      export_full_dataset_csv,
    "wigle":     export_wigle_csv,
    "ap_status": export_ap_status_csv,
}

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
        description="Экспорт данных wifi-monitor в CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
профили:
  heatmap    Данные для тепловой карты в QGIS (только точки с GPS)
  full       Полный датасет: все поля networks + observations
  wigle      Формат WigleWifi-1.4 для загрузки на wigle.net
  ap_status  Результаты проверки точек оператора

примеры:
  python -m src.exporter --profile heatmap
  python -m src.exporter --profile heatmap --bssid AA:BB:CC:DD:EE:FF
  python -m src.exporter --profile wigle --db data/wifi_monitor.db
  python -m src.exporter --profile ap_status --out /tmp/report.csv
        """,
    )
    parser.add_argument(
        "--profile", required=True, choices=_PROFILES.keys(),
        metavar="<профиль>",
        help="heatmap | full | wigle | ap_status",
    )
    parser.add_argument(
        "--bssid",
        metavar="AA:BB:CC:DD:EE:FF",
        help="Фильтр по BSSID (только для профиля heatmap)",
    )
    parser.add_argument(
        "--db", default=_default_db,
        metavar="<путь>",
        help="Путь к SQLite базе (default: {})".format(_default_db),
    )
    parser.add_argument(
        "--out",
        metavar="<путь>",
        help="Путь к выходному файлу (default: export/<профиль>_<timestamp>.csv)",
    )

    args = parser.parse_args()

    # Валидация: --bssid только для heatmap
    if args.bssid and args.profile != "heatmap":
        parser.error("--bssid можно использовать только с --profile heatmap")

    # Путь к выходному файлу
    out_path = args.out or "export/{}_{}.csv".format(args.profile, _ts())

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

    try:
        fn = _PROFILES[args.profile]

        # heatmap принимает дополнительный аргумент bssid_filter
        if args.profile == "heatmap":
            count = fn(conn, out_path, bssid_filter=args.bssid)
        else:
            count = fn(conn, out_path)

        print("Экспортировано {} строк → {}".format(count, out_path))

    except sqlite3.Error as exc:
        print("Ошибка базы данных: {}".format(exc))
        sys.exit(1)
    except OSError as exc:
        print("Ошибка записи файла: {}".format(exc))
        sys.exit(1)
    finally:
        conn.close()