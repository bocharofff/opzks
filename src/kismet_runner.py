# -*- coding: utf-8 -*-
"""
src/kismet_runner.py

Запускает Kismet как дочерний процесс, читает его SQLite-базу
и синхронизирует данные в нашу БД.

Используется из src/cli.py (режимы 1 и 3).
"""

import glob
import json
import logging
import os
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
    empty = {
        "ssid": None, "encryption": None,
        "manufacturer": None, "channel": None, "frequency": None,
    }
    if not device_json:
        return empty
    try:
        blob = json.loads(device_json)
        # channel может быть строкой ("6") или числом — приводим к int
        raw_channel = blob.get("kismet.device.base.channel")
        try:
            channel = int(raw_channel) if raw_channel is not None else None
        except (ValueError, TypeError):
            channel = None

        raw_freq = blob.get("kismet.device.base.frequency")
        try:
            frequency = float(raw_freq) if raw_freq is not None else None
        except (ValueError, TypeError):
            frequency = None

        # frequency хранится в кГц (например 2412000) — переводим в МГц
        if frequency is not None:
            frequency = frequency / 1000.0

        return {
            "ssid":         blob.get("kismet.device.base.commonname"),
            "encryption":   blob.get("kismet.device.base.crypt"),   # не crypt_string
            "manufacturer": blob.get("kismet.device.base.manuf"),
            "channel":      channel,
            "frequency":    frequency,
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

def find_kismet_db(log_dir="data/", title="wardrive"):
    """
    Находит самый свежий .kismet файл в log_dir с заданным title.
    Kismet создаёт базы по схеме: <log_dir><title>-YYYYMMDD-HH-MM-SS-N.kismet
    Возвращает путь к файлу или None если не найден.
    """
    pattern = os.path.join(log_dir, "{}-*.kismet".format(title))
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Берём самый свежий по времени изменения
    return max(matches, key=os.path.getmtime)


def start_kismet(monitor_iface, log_dir="data/", title="wardrive"):
    """
    Запускает Kismet как фоновый процесс в wardrive-режиме.

    Kismet сам генерирует имя базы: <log_dir><title>-YYYYMMDD-HH-MM-SS-1.kismet
    После старта находит созданный файл через find_kismet_db().

    Возвращает кортеж (proc, kismet_db_path).
    Бросает RuntimeError если Kismet упал или база не создалась.
    """
    os.makedirs(log_dir, exist_ok=True)

    # --daemonize НЕ используем: launcher завершится с кодом 0 и мы потеряем proc.
    # --override wardrive включает kismetdb-логирование и отключает pcap.
    # --log-types НЕ используем: в разных версиях Kismet этот флаг ведёт себя
    #   непредсказуемо и может конфликтовать с --override wardrive.
    # --log-prefix передаём как абсолютный путь, чтобы Kismet не искал файл
    #   относительно своего рабочего каталога.
    abs_log_dir = os.path.abspath(log_dir)
    if not abs_log_dir.endswith(os.sep):
        abs_log_dir += os.sep

    cmd = [
        "kismet",
        "-c", monitor_iface,
        "--no-ncurses",
        "--override", "wardrive",
        "--log-prefix", abs_log_dir,
        "--log-title", title,
    ]

    # stdout/stderr в файлы: при PIPE Python блокируется если буфер переполнится
    stdout_log = os.path.join(log_dir, "kismet_stdout.log")
    stderr_log = os.path.join(log_dir, "kismet_stderr.log")

    logger.info("Запуск Kismet: %s", " ".join(cmd))
    logger.info("Логи Kismet: %s / %s", stdout_log, stderr_log)

    try:
        fout = open(stdout_log, "w")
        ferr = open(stderr_log, "w")
        proc = subprocess.Popen(cmd, stdout=fout, stderr=ferr)
    except FileNotFoundError:
        raise RuntimeError("Kismet не найден. Установите: sudo apt install kismet")

    # Kismet создаёт файл базы при первой записи (интервал до 30 сек).
    # Ждём до 45 сек, проверяя каждые 2 сек. Ищем файл как в log_dir,
    # так и в ~/.kismet/ — Kismet иногда игнорирует --log-prefix
    # и пишет туда по умолчанию.
    fallback_dirs = [
        os.path.abspath(log_dir),
        os.path.expanduser("~/.kismet"),
        os.path.expanduser("~/kismet"),
        os.getcwd(),
    ]
    db_path = None
    max_attempts = 23  # 23 × 2 сек ≈ 45 сек
    for attempt in range(max_attempts):
        time.sleep(2)
        if proc.poll() is not None:
            try:
                stderr_text = open(stderr_log).read().strip()
            except OSError:
                stderr_text = "(лог недоступен)"
            raise RuntimeError(
                "Kismet завершился при старте (код {}): {}".format(
                    proc.returncode, stderr_text
                )
            )
        # Ищем файл во всех возможных местах
        for search_dir in fallback_dirs:
            found = find_kismet_db(search_dir, title)
            if found:
                db_path = found
                break
        if db_path:
            logger.info(
                "База Kismet создана: %s (попытка %d/%d)",
                db_path, attempt + 1, max_attempts
            )
            break
        if attempt % 5 == 0:
            logger.info(
                "Ожидание базы Kismet... %d/%d сек",
                (attempt + 1) * 2, max_attempts * 2
            )

    if not db_path:
        proc.terminate()
        # Сообщаем где именно искали
        searched = ", ".join(fallback_dirs)
        raise RuntimeError(
            "Kismet запущен (PID {}), но база не появилась за {} сек. "
            "Искали в: {}. Проверьте логи: {}".format(
                proc.pid, max_attempts * 2, searched, stderr_log
            )
        )

    logger.info("Kismet запущен (PID %d), база: %s", proc.pid, db_path)
    return proc, db_path


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

    # mode=ro — только чтение, без immutable: Kismet держит файл открытым
    # на запись, immutable=1 заставляет SQLite читать устаревший page cache
    # и может приводить к пустым результатам при активной записи.
    uri = "file:{}?mode=ro".format(kismet_db)
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        logger.error("Не удалось открыть базу Kismet (%s): %s", kismet_db, exc)
        return []

    try:
        # Реальная схема таблицы devices (проверено на живой базе Kismet):
        # first_time, last_time, devkey, phyname, devmac, strongest_signal,
        # min_lat, min_lon, max_lat, max_lon, avg_lat, avg_lon, bytes_data, type, device
        # Колонок min_channel/max_channel/min_freq/max_freq нет —
        # канал и частота берутся из JSON blob.
        rows = conn.execute(
            """
            SELECT devmac,
                   avg_lat,  avg_lon,
                   min_lat,  min_lon,
                   max_lat,  max_lon,
                   strongest_signal,
                   first_time, last_time,
                   device
            FROM   devices
            WHERE  type = 'Wi-Fi AP'
              AND  last_time > ?
            """,
            (int(since_ts),),
        ).fetchall()
        logger.debug("SQL вернул %d строк из базы Kismet", len(rows))
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
                # channel и frequency — только в JSON blob, не в отдельных колонках
                "channel":      blob_data.get("channel"),
                "frequency":    blob_data.get("frequency"),
                "first_seen":   _ts_to_iso(row["first_time"]),
                "last_seen":    _ts_to_iso(row["last_time"]),
                "best_lat":     lat if _coords_valid(lat, lon) else None,
                "best_lon":     lon if _coords_valid(lat, lon) else None,
                "best_signal":  row["strongest_signal"],
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
Использование для тестирования:
  sudo python3 -m src.kismet_runner <интерфейс_монитора> [папка_логов]
Пример:
  sudo python3 -m src.kismet_runner wlan0mon data/
""".strip()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    monitor_iface = sys.argv[1]
    # Если папка не задана — по умолчанию data/
    log_dir = sys.argv[2] if len(sys.argv) > 2 else "data/"
    kismet_db_path = None
    proc = None

    try:
        # 1. Запуск Kismet
        logger.info("=== ШАГ 1: Запуск Kismet на интерфейсе %s ===", monitor_iface)
        # start_kismet возвращает (proc, db_path) — Kismet сам генерирует имя файла
        proc, kismet_db_path = start_kismet(monitor_iface=monitor_iface, log_dir=log_dir)

        # 2. Сбор данных
        scan_duration = 15
        logger.info("=== ШАГ 2: Ждем %d секунд, пока Kismet собирает сети... ===", scan_duration)
        for i in range(scan_duration, 0, -1):
            sys.stdout.write("\rОсталось времени сборки: {} сек... ".format(i))
            sys.stdout.flush()
            time.sleep(1)
        print("\n")

    except KeyboardInterrupt:
        logger.info("\nТест прерван пользователем (Ctrl+C)")
    except Exception as e:
        logger.error("Произошла ошибка во время теста: %s", e)
    finally:
        # 3. Сначала останавливаем Kismet — он сбрасывает все данные на диск
        if proc is not None:
            logger.info("=== ШАГ 3: Остановка Kismet (сброс данных на диск) ===")
            stop_kismet(proc)
            proc = None

    # 4. Читаем базу только после остановки — все данные уже на диске
    if kismet_db_path:
        logger.info("=== ШАГ 4: Чтение базы Kismet: %s ===", kismet_db_path)
        nets = read_kismet_networks(kismet_db_path, since_ts=0.0)

        if not nets:
            logger.warning("Сетей не найдено. Проверьте что интерфейс был в monitor mode.")
        else:
            print("\n" + "=" * 90)
            print("РЕЗУЛЬТАТЫ СКАНИРОВАНИЯ (Найдено сетей: {})".format(len(nets)))
            print("=" * 90)
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
                    str(round(n["best_lat"], 5)) if n["best_lat"] else "No GPS",
                    str(round(n["best_lon"], 5)) if n["best_lon"] else "No GPS",
                ))
            print("=" * 90 + "\n")