# -*- coding: utf-8 -*-
"""
src/kismet_runner.py

Запускает Kismet как дочерний процесс, читает его SQLite-базу
и синхронизирует данные в нашу БД.

Используется из src/cli.py (режимы 1 и 3).
"""

import json
import logging
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

from src.db import checkpoint, insert_observation, upsert_network

logger = logging.getLogger(__name__)

# Количество вставленных observations между checkpoint-ами WAL
_CHECKPOINT_EVERY = 100


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _ts_to_iso(unix_ts):
    """Конвертирует unix timestamp (int) в строку ISO8601 UTC."""
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_blob(device_json):
    """
    Парсит JSON blob из поля device таблицы devices.

    Возвращает словарь с полями ssid, encryption, manufacturer.
    При ошибке парсинга возвращает словарь с None-значениями.
    """
    empty = {"ssid": None, "encryption": None, "manufacturer": None}
    if not device_json:
        return empty
    try:
        blob = json.loads(device_json)
        return {
            "ssid":         blob.get("kismet.device.base.commonname"),
            "encryption":   blob.get("kismet.device.base.crypt_string"),
            "manufacturer": blob.get("kismet.device.base.manuf"),
        }
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Не удалось распарсить JSON blob устройства: %s", exc)
        return empty


def _coords_valid(lat, lon):
    """
    Проверяет что координаты реальные.
    Kismet пишет 0.0 / 0.0 если GPS-фикса не было — такие координаты
    не имеют смысла и лучше их игнорировать.
    """
    if lat is None or lon is None:
        return False
    return not (lat == 0.0 and lon == 0.0)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def start_kismet(monitor_iface, kismet_db_path, log_dir="data/"):
    """
    Запускает Kismet как фоновый процесс в wardrive-режиме.

    Ждёт 3 секунды и проверяет что процесс жив.
    Возвращает объект Popen.
    Бросает RuntimeError если Kismet упал сразу после запуска.
    """
    import os
    os.makedirs(log_dir, exist_ok=True)

    cmd = [
        "kismet",
        "-c", monitor_iface,
        "--no-ncurses",
        "--daemonize",
        "--kismetdb", kismet_db_path,
        "--override", "wardrive",
        "--log-prefix", log_dir,
    ]
    logger.info("Запуск Kismet: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("Kismet не найден. Установите: sudo apt install kismet")

    # Даём Kismet время инициализироваться
    time.sleep(3)

    if proc.poll() is not None:
        # Процесс уже завершился — что-то пошло не так
        _, stderr = proc.communicate()
        raise RuntimeError(
            "Kismet завершился сразу после запуска (код {}): {}".format(
                proc.returncode, stderr.decode(errors="replace").strip()
            )
        )

    logger.info("Kismet запущен (PID %d), база: %s", proc.pid, kismet_db_path)
    return proc


def stop_kismet(proc):
    """
    Останавливает Kismet: SIGTERM → ждёт 5 сек → SIGKILL если завис.
    Не бросает исключений.
    """
    if proc is None:
        return

    logger.info("Остановка Kismet (PID %d)...", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=5)
        logger.info("Kismet завершён штатно")
    except subprocess.TimeoutExpired:
        logger.warning("Kismet не завершился за 5 сек — отправляем SIGKILL")
        proc.kill()
        proc.wait()
        logger.info("Kismet принудительно остановлен")
    except Exception as exc:
        logger.error("Ошибка при остановке Kismet: %s", exc)


def read_kismet_networks(kismet_db, since_ts=0.0):
    """
    Читает устройства типа 'Wi-Fi AP' из Kismet SQLite базы.

    База открывается строго на чтение (mode=ro&immutable=1).
    Возвращает list[dict] с полями:
        bssid, ssid, encryption, manufacturer, channel, frequency,
        first_seen, last_seen, best_lat, best_lon, best_signal
    При ошибке возвращает [].
    """
    if not kismet_db:
        logger.error("read_kismet_networks: путь к базе Kismet не задан")
        return []

    import os
    if not os.path.exists(kismet_db):
        logger.warning("База Kismet не найдена: %s", kismet_db)
        return []

    uri = "file:{}?mode=ro&immutable=1".format(kismet_db)
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        logger.error("Не удалось открыть базу Kismet (%s): %s", kismet_db, exc)
        return []

    try:
        rows = conn.execute(
            """
            SELECT devmac,
                   avg_lat,    avg_lon,
                   min_lat,    min_lon,
                   max_lat,    max_lon,
                   best_signal,
                   min_freq,   max_freq,
                   min_channel, max_channel,
                   first_time, last_time,
                   device
            FROM   devices
            WHERE  type = 'Wi-Fi AP'
              AND  last_time > ?
            """,
            (int(since_ts),),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error("Ошибка SQL при чтении базы Kismet: %s", exc)
        conn.close()
        return []

    networks = []
    for row in rows:
        try:
            blob_data = _extract_blob(row["device"])

            # Предпочитаем avg-координаты; если нулевые — пробуем max
            lat = row["avg_lat"]
            lon = row["avg_lon"]
            if not _coords_valid(lat, lon):
                lat = row["max_lat"]
                lon = row["max_lon"]

            networks.append({
                "bssid":        row["devmac"],
                "ssid":         blob_data["ssid"],
                "encryption":   blob_data["encryption"],
                "manufacturer": blob_data["manufacturer"],
                # Берём минимальный канал (обычно он один и тот же)
                "channel":   row["min_channel"] or row["max_channel"],
                "frequency": row["min_freq"] or row["max_freq"],
                "first_seen": _ts_to_iso(row["first_time"]),
                "last_seen":  _ts_to_iso(row["last_time"]),
                "best_lat":   lat if _coords_valid(lat, lon) else None,
                "best_lon":   lon if _coords_valid(lat, lon) else None,
                "best_signal": row["best_signal"],
            })
        except Exception as exc:
            logger.warning(
                "Пропускаем устройство %s: ошибка парсинга: %s",
                row["devmac"] if "devmac" in row.keys() else "?",
                exc,
            )

    conn.close()
    logger.debug("Прочитано %d сетей из Kismet (since_ts=%.0f)", len(networks), since_ts)
    return networks


def sync_kismet_to_db(kismet_db, conn, gps, since_ts=0.0):
    """
    Читает новые сети из Kismet и сохраняет их в нашу БД.

    Координаты наблюдения:
      1. best_lat/lon из Kismet (если не нулевые)
      2. gps.latest() как резерв
      3. None если ни то, ни другое не доступно

    Делает один conn.commit() в конце.
    WAL checkpoint каждые 100 вставленных observations.
    Возвращает количество вставленных observations.
    """
    networks = read_kismet_networks(kismet_db, since_ts)
    if not networks:
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    gps_pos = gps.latest() if gps is not None else None

    count = 0
    for net in networks:
        try:
            # 1. Обновляем или вставляем запись о сети
            upsert_network(conn, {
                "bssid":        net["bssid"],
                "ssid":         net["ssid"],
                "encryption":   net["encryption"],
                "manufacturer": net["manufacturer"],
                "channel":      net["channel"],
                "frequency":    net["frequency"],
            })

            # 2. Определяем координаты для наблюдения
            lat, lon = None, None
            if net["best_lat"] is not None and net["best_lon"] is not None:
                # Kismet знает где видел эту точку — используем его данные
                lat = net["best_lat"]
                lon = net["best_lon"]
            elif gps_pos is not None:
                # Kismet не дал координаты — берём текущее положение из GPS
                lat = gps_pos["lat"]
                lon = gps_pos["lon"]

            # 3. Записываем наблюдение
            insert_observation(conn, {
                "bssid":     net["bssid"],
                "timestamp": net["last_seen"] or now,
                "lat":       lat,
                "lon":       lon,
                "rssi":      net["best_signal"],
                "channel":   net["channel"],
                "frequency": net["frequency"],
            })
            count += 1

            # WAL checkpoint чтобы не копился большой журнал
            if count % _CHECKPOINT_EVERY == 0:
                checkpoint(conn)
                logger.debug("WAL checkpoint после %d записей", count)

        except Exception as exc:
            logger.error(
                "Ошибка при синхронизации сети %s: %s", net.get("bssid"), exc
            )

    try:
        conn.commit()
    except Exception as exc:
        logger.error("Ошибка при commit после синхронизации: %s", exc)

    logger.info(
        "Синхронизация завершена: %d observations, %d сетей из Kismet",
        count, len(networks),
    )
    return count


# ---------------------------------------------------------------------------
# Запуск из командной строки
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    USAGE = """
Использование (вспомогательный запуск, не требует Kismet):

  python -m src.kismet_runner read <kismet_db> [since_ts]
      Прочитать сети из базы Kismet и вывести их в stdout.
      since_ts — unix timestamp, читать только записи новее него (default: 0)

Примеры:
  python -m src.kismet_runner read data/kismet.kismet
  python -m src.kismet_runner read data/kismet.kismet 1700000000

Управление процессом Kismet производится через src/cli.py:
  sudo python -m src.cli --mode 1 --monitor wlan0
""".strip()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) < 3 or sys.argv[1] != "read":
        print(USAGE)
        sys.exit(0)

    db_path = sys.argv[2]
    ts = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    nets = read_kismet_networks(db_path, since_ts=ts)
    if not nets:
        print("Сетей не найдено (база пуста или файл не существует).")
        sys.exit(0)

    print("Найдено сетей: {}".format(len(nets)))
    print("{:<20} {:<32} {:<10} {:<8} {:<10} {:<10}".format(
        "BSSID", "SSID", "Шифр.", "Канал", "Шир.", "Долг."
    ))
    print("-" * 90)
    for n in nets:
        print("{:<20} {:<32} {:<10} {:<8} {:<10} {:<10}".format(
            n["bssid"] or "",
            (n["ssid"] or "")[:31],
            (n["encryption"] or "")[:9],
            str(n["channel"] or ""),
            str(round(n["best_lat"], 5)) if n["best_lat"] else "",
            str(round(n["best_lon"], 5)) if n["best_lon"] else "",
        ))