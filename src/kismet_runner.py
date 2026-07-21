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

def find_kismet_db(log_dir="data/", title="scan_wifi"):
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


def start_kismet(monitor_iface, log_dir="data/", title="scan_wifi",
                 channels=None):
    """
    Запускает Kismet как фоновый процесс.

    Kismet сам генерирует имя базы: <log_dir><title>-YYYYMMDD-HH-MM-SS-1.kismet
    После старта находит созданный файл через find_kismet_db().

    channels — список каналов для сканирования, например [1, 6, 11, 36, 44, 149].
    Если None — Kismet делает hopping по всем каналам (может пропускать сети
    на 5 ГГц из-за большого числа каналов и короткого времени на каждом).
    Один канал (например channels=[44]) — фиксирует адаптер на нём.

    Возвращает кортеж (proc, kismet_db_path).
    Бросает RuntimeError если Kismet упал или база не создалась.
    """
    os.makedirs(log_dir, exist_ok=True)

    abs_log_dir = os.path.abspath(log_dir)
    if not abs_log_dir.endswith(os.sep):
        abs_log_dir += os.sep

    # Формируем строку источника: wlan0 или "wlan0:channels=1,6,11,44"
    if channels:
        ch_str = ",".join(str(c) for c in channels)
        source = "{}:channels={}".format(monitor_iface, ch_str)
        logger.debug("Kismet: список каналов: %s", ch_str)
    else:
        source = monitor_iface
        logger.debug("Kismet: автоматический hopping по всем каналам")

    cmd = [
        "kismet",
        "-c", source,
        "--no-ncurses",
        # "--override", "wardrive",
        "--log-prefix", abs_log_dir,
        "--log-title", title,
    ]

    # stdout/stderr в файлы: при PIPE Python блокируется если буфер переполнится
    stdout_log = os.path.join(log_dir, "kismet_stdout.log")
    stderr_log = os.path.join(log_dir, "kismet_stderr.log")

    logger.debug("Запуск Kismet: %s", " ".join(cmd))
    logger.debug("Логи Kismet: %s / %s", stdout_log, stderr_log)

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
            logger.debug(
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


def _open_kismet_ro(kismet_db):
    """Открывает базу Kismet строго на чтение (mode=ro). None при ошибке.

    Без immutable=1: Kismet держит файл открытым на запись, а immutable
    заставляет SQLite читать устаревший page cache (пустые результаты при
    активной записи).
    """
    if not kismet_db:
        logger.error("Путь к базе Kismet не задан")
        return None
    if not os.path.exists(kismet_db):
        logger.warning("База Kismet не найдена: %s", kismet_db)
        return None
    try:
        conn = sqlite3.connect("file:{}?mode=ro".format(kismet_db), uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as exc:
        logger.error("Не удалось открыть базу Kismet (%s): %s", kismet_db, exc)
        return None


def read_ap_metadata(kismet_db):
    """Читает МЕТАДАННЫЕ точек доступа из таблицы ``devices`` (type='Wi-Fi AP').

    Координаты/сигнал из ``devices`` (центроид ``avg_lat/avg_lon``,
    ``strongest_signal``) НЕ используются — это оценка расположения AP, а нам
    нужна наша позиция в момент приёма (она берётся из таблицы ``packets``).
    Здесь — только стабильные атрибуты сети.

    Args:
        kismet_db: Путь к ``.kismet`` файлу.

    Returns:
        ``{BSSID_UPPER: {ssid, encryption, manufacturer, channel, frequency}}``.
        Пустой словарь при ошибке.
    """
    conn = _open_kismet_ro(kismet_db)
    if conn is None:
        return {}

    meta = {}
    try:
        rows = conn.execute(
            "SELECT devmac, device FROM devices WHERE type = 'Wi-Fi AP'"
        ).fetchall()
        for row in rows:
            try:
                blob = _extract_blob(row["device"])
                meta[row["devmac"].upper()] = {
                    "ssid":         blob["ssid"],
                    "encryption":   blob["encryption"],
                    "manufacturer": blob["manufacturer"],
                    "channel":      blob.get("channel"),
                    "frequency":    blob.get("frequency"),
                }
            except Exception as exc:
                logger.warning("Пропуск устройства при чтении метаданных: %s", exc)
    except sqlite3.Error as exc:
        logger.error("Ошибка SQL при чтении devices: %s", exc)
    finally:
        conn.close()

    logger.debug("Метаданные: %d точек доступа", len(meta))
    return meta


def read_kismet_packets(kismet_db, since_packetid=0, bucket_sec=1):
    """Читает НОВЫЕ сэмплы сигнала из таблицы ``packets`` (для тепловой карты).

    Каждый beacon/кадр, ИСХОДЯЩИЙ от точки доступа, несёт: сигнал (dBm) и
    lat/lon — позицию ПРИЁМНИКА (нас) в момент захвата (Kismet штампует из gpsd).
    Это и есть «где мы были, когда услышали сеть». Читаем инкрементально по
    ``packetid`` и даунсэмплим до 1 сэмпла на ``bucket_sec`` секунд на BSSID
    (берём сильнейший сигнал в бакете; lat/lon — из строки с этим MAX).

    Фильтр ``sourcemac IN (SELECT devmac FROM devices WHERE type='Wi-Fi AP')``
    оставляет только кадры от известных AP и не зависит от лимита параметров.

    Args:
        kismet_db:      Путь к ``.kismet`` файлу.
        since_packetid: Читать пакеты с ``packetid`` строго больше этого значения.
        bucket_sec:     Ширина бакета даунсэмплинга в секундах (>=1).

    Returns:
        Кортеж ``(samples, last_packetid)``:
          - ``samples``       — список ``{bssid, signal, lat, lon, ts_sec, frequency}``;
          - ``last_packetid`` — новый водораздел (max packetid на момент чтения),
            который нужно передать в следующий вызов. При отсутствии новых данных
            равен входному ``since_packetid``.
    """
    conn = _open_kismet_ro(kismet_db)
    if conn is None:
        return [], since_packetid

    bucket = max(1, int(bucket_sec))
    try:
        row = conn.execute("SELECT MAX(packetid) AS m FROM packets").fetchone()
        snapshot_max = row["m"] if row and row["m"] is not None else None
        if snapshot_max is None or snapshot_max <= since_packetid:
            return [], since_packetid

        rows = conn.execute(
            """
            SELECT sourcemac      AS bssid,
                   MAX(signal)    AS signal,
                   lat, lon,
                   ts_sec,
                   frequency
            FROM   packets
            WHERE  packetid > :since
              AND  packetid <= :snap
              AND  phyname = 'IEEE802.11'
              AND  signal <> 0
              AND  sourcemac IN (
                       SELECT devmac FROM devices WHERE type = 'Wi-Fi AP'
                   )
            GROUP  BY sourcemac, ts_sec / :bucket
            ORDER  BY ts_sec
            """,
            {"since": since_packetid, "snap": snapshot_max, "bucket": bucket},
        ).fetchall()
    except sqlite3.Error as exc:
        logger.error("Ошибка SQL при чтении packets: %s", exc)
        conn.close()
        return [], since_packetid

    samples = []
    for r in rows:
        # frequency в packets хранится в кГц — переводим в МГц (как в _extract_blob)
        freq = r["frequency"]
        try:
            freq_mhz = float(freq) / 1000.0 if freq is not None else None
        except (TypeError, ValueError):
            freq_mhz = None
        samples.append({
            "bssid":     r["bssid"],
            "signal":    r["signal"],
            "lat":       r["lat"],
            "lon":       r["lon"],
            "ts_sec":    r["ts_sec"],
            "frequency": freq_mhz,
        })

    conn.close()
    logger.debug(
        "packets: %d сэмплов (packetid %s..%s, bucket=%ds)",
        len(samples), since_packetid, snapshot_max, bucket,
    )
    return samples, snapshot_max


def sync_kismet_to_db(kismet_db, conn, gps, since_packetid=0, bucket_sec=1):
    """Синхронизирует новые данные Kismet в нашу БД: метаданные сетей + сэмплы сигнала.

    Наблюдение = сэмпл сигнала в точке пространства (для тепловой карты).
    Координата берётся по времени пакета (см. модульный docstring раздела 1 плана):
      1. валидные ``lat/lon`` из ``packets`` (штамп Kismet = наша позиция тогда);
      2. иначе ``gps.position_at(ts_sec)`` — наша позиция из трека по времени пакета;
      3. иначе координат нет → ``has_gps=0`` (в тепловую карту не попадёт).
    ``gps.latest()`` (позиция «сейчас») к историческим пакетам НЕ привязывается.

    Args:
        kismet_db:      Путь к ``.kismet`` файлу.
        conn:           Соединение с нашей БД.
        gps:            ``GPSMonitor`` или ``None``.
        since_packetid: Водораздел прошлой синхронизации.
        bucket_sec:     Ширина бакета даунсэмплинга, секунды.

    Returns:
        Кортеж ``(count, last_packetid)`` — сколько наблюдений вставлено и новый
        водораздел для следующего вызова.
    """
    meta = read_ap_metadata(kismet_db)

    # 1. Обновляем метаданные всех известных сетей (идемпотентно)
    upserted = set()
    for bssid_up, m in meta.items():
        try:
            upsert_network(conn, {
                "bssid":        bssid_up,
                "ssid":         m["ssid"],
                "encryption":   m["encryption"],
                "manufacturer": m["manufacturer"],
                "channel":      m["channel"],
                "frequency":    m["frequency"],
            })
            upserted.add(bssid_up)
        except Exception as exc:
            logger.error("Ошибка upsert_network %s: %s", bssid_up, exc)

    # 2. Читаем новые сэмплы сигнала из packets
    samples, last_packetid = read_kismet_packets(kismet_db, since_packetid, bucket_sec)
    if not samples:
        try:
            conn.commit()
        except Exception as exc:
            logger.error("Ошибка commit при синхронизации (без сэмплов): %s", exc)
        return 0, last_packetid

    count = 0
    for s in samples:
        try:
            bssid = (s["bssid"] or "").upper()
            if not bssid:
                continue

            # На случай гонки devices/packets — гарантируем наличие строки сети (FK)
            if bssid not in upserted:
                m = meta.get(bssid, {})
                upsert_network(conn, {
                    "bssid":        bssid,
                    "ssid":         m.get("ssid"),
                    "encryption":   m.get("encryption"),
                    "manufacturer": m.get("manufacturer"),
                    "channel":      m.get("channel"),
                    "frequency":    m.get("frequency"),
                })
                upserted.add(bssid)

            # Координата — по времени пакета, приоритетно из штампа Kismet
            lat, lon = s["lat"], s["lon"]
            if not _coords_valid(lat, lon):
                lat, lon = None, None
                if gps is not None:
                    pos = gps.position_at(s["ts_sec"])
                    if pos is not None:
                        lat, lon = pos["lat"], pos["lon"]

            insert_observation(conn, {
                "bssid":     bssid,
                "timestamp": _ts_to_iso(s["ts_sec"]),
                "lat":       lat,
                "lon":       lon,
                "rssi":      s["signal"],
                "channel":   meta.get(bssid, {}).get("channel"),
                "frequency": s["frequency"],
            })
            count += 1

            if count % _CHECKPOINT_EVERY == 0:
                checkpoint(conn)
                logger.debug("WAL checkpoint после %d записей", count)

        except Exception as exc:
            logger.error("Ошибка при вставке сэмпла %s: %s", s.get("bssid"), exc)

    try:
        conn.commit()
    except Exception as exc:
        logger.error("Ошибка при commit после синхронизации: %s", exc)

    logger.debug(
        "Синхронизация: %d наблюдений, %d сетей (packetid → %s)",
        count, len(meta), last_packetid,
    )
    return count, last_packetid


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
    # Если каналы не заданы — автоматический hopping
    # Пример: sudo python3 -m src.kismet_runner wlan0 data/ 1,6,11,36,44,149
    channels = None
    if len(sys.argv) > 3:
        channels = [int(c.strip()) for c in sys.argv[3].split(",")]
        logger.info("Каналы для сканирования: %s", channels)

    kismet_db_path = None
    proc = None

    try:
        # 1. Запуск Kismet
        logger.info("=== ШАГ 1: Запуск Kismet на интерфейсе %s ===", monitor_iface)
        proc, kismet_db_path = start_kismet(
            monitor_iface=monitor_iface,
            log_dir=log_dir,
            channels=channels,
        )

        # 2. Сбор данных
        # Ждём появления данных в базе — Kismet делает flush каждые 30 сек.
        # Вместо слепого таймера: проверяем базу каждые 5 сек после первого flush.
        # Минимум 35 сек (чтобы гарантированно прошёл первый flush),
        # максимум 120 сек (защита от зависания).
        min_wait   = 35
        max_wait   = 120
        check_interval = 5
        elapsed = 0

        logger.info("=== ШАГ 2: Сбор данных (мин. %d сек, макс. %d сек) ===",
                    min_wait, max_wait)

        last_count = 0
        while elapsed < max_wait:
            time.sleep(check_interval)
            elapsed += check_interval
            sys.stdout.write("\r  Прошло {} сек... ".format(elapsed))
            sys.stdout.flush()

            # Начинаем проверять базу только после min_wait
            if elapsed < min_wait:
                continue

            # Читаем базу прямо сейчас (Kismet держит её открытой, но flush уже был)
            try:
                current = read_ap_metadata(kismet_db_path)
                count = len(current)
            except Exception:
                count = 0

            if count > 0:
                if count == last_count:
                    # Данные есть и не прибавляются — можно останавливать
                    logger.info("\n  Найдено %d сетей, данные стабильны — останавливаем", count)
                    break
                else:
                    logger.info("\n  Найдено %d сетей, ждём ещё %d сек...", count, check_interval)
                    last_count = count

        print("")

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
        meta = read_ap_metadata(kismet_db_path)
        samples, last_pid = read_kismet_packets(kismet_db_path, since_packetid=0, bucket_sec=1)

        if not meta:
            logger.warning("Сетей не найдено. Проверьте что интерфейс был в monitor mode.")
        else:
            # Считаем сэмплы (пар «координата→сигнал») на каждую сеть — это и есть
            # объём данных для тепловой карты.
            per_bssid = {}
            for s in samples:
                b = (s["bssid"] or "").upper()
                per_bssid[b] = per_bssid.get(b, 0) + 1

            print("\n" + "=" * 92)
            print("СЕТИ: {}   СЭМПЛОВ СИГНАЛА: {}   (packetid → {})".format(
                len(meta), len(samples), last_pid))
            print("=" * 92)
            print("{:<20} {:<32} {:<10} {:<8} {:<10}".format(
                "BSSID", "SSID", "Шифр.", "Канал", "Сэмплов"
            ))
            print("-" * 92)
            for bssid, m in meta.items():
                print("{:<20} {:<32} {:<10} {:<8} {:<10}".format(
                    bssid,
                    (m["ssid"] or "")[:31],
                    (m["encryption"] or "")[:9],
                    str(m["channel"] or ""),
                    per_bssid.get(bssid, 0),
                ))
            print("=" * 92 + "\n")