"""
src/db.py — инициализация SQLite, upsert/insert-хелперы, WAL-checkpoint.

Используется из: kismet_runner.py, ap_checker.py, exporter.py.
Не импортирует ничего из других модулей проекта.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Возвращает текущее время в UTC в формате ISO 8601 с суффиксом Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    """Открывает (или создаёт) SQLite-базу и инициализирует схему.

    Действия:
    - Создаёт директорию для файла БД при необходимости.
    - Включает WAL mode и поддержку внешних ключей.
    - Создаёт таблицы ``networks``, ``observations``, ``ap_health``
      (CREATE TABLE IF NOT EXISTS — идемпотентно).
    - Устанавливает ``row_factory = sqlite3.Row``, чтобы строки
      были доступны как по индексу, так и по имени колонки.

    Args:
        path: Путь к файлу SQLite (например ``"data/wifi_monitor.db"``).

    Returns:
        Открытый объект :class:`sqlite3.Connection`.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS networks (
            bssid        TEXT PRIMARY KEY,
            ssid         TEXT,
            encryption   TEXT,
            manufacturer TEXT,
            channel      INTEGER,
            frequency    REAL,
            first_seen   TEXT,
            last_seen    TEXT
        );

        CREATE TABLE IF NOT EXISTS observations (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            bssid     TEXT REFERENCES networks(bssid),
            timestamp TEXT,
            lat       REAL,
            lon       REAL,
            rssi      INTEGER,
            channel   INTEGER,
            frequency REAL,
            has_gps   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ap_health (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ap_id     TEXT,
            timestamp TEXT,
            status    TEXT,
            rtt_ms    REAL,
            lat       REAL,
            lon       REAL,
            has_gps   INTEGER DEFAULT 0
        );
    """)

    conn.commit()
    logger.debug("БД инициализирована: %s", path)
    return conn


def upsert_network(conn: sqlite3.Connection, data: dict) -> None:
    """Добавляет или обновляет запись о сети в таблице ``networks``.

    При вставке нового BSSID поля ``first_seen`` и ``last_seen`` устанавливаются
    в текущее время UTC.  При обновлении существующего BSSID ``first_seen``
    сохраняется через подзапрос ``COALESCE``, ``last_seen`` обновляется.

    Транзакцию **не фиксирует** — commit на стороне вызывающего кода.

    Args:
        conn: Активное соединение с БД.
        data: Словарь с полями сети:

            - ``bssid``        (str, обязательный) — MAC точки доступа;
            - ``ssid``         (str, опциональный);
            - ``encryption``   (str, опциональный) — Open/WEP/WPA/WPA2/WPA3;
            - ``manufacturer`` (str, опциональный);
            - ``channel``      (int, опциональный);
            - ``frequency``    (float, опциональный).
    """
    now = _now_iso()

    conn.execute(
        """
        INSERT INTO networks (bssid, ssid, encryption, manufacturer,
                              channel, frequency, first_seen, last_seen)
        VALUES (
            :bssid, :ssid, :encryption, :manufacturer,
            :channel, :frequency,
            -- Сохраняем first_seen если запись уже существует
            COALESCE(
                (SELECT first_seen FROM networks WHERE bssid = :bssid),
                :now
            ),
            :now
        )
        ON CONFLICT(bssid) DO UPDATE SET
            ssid         = excluded.ssid,
            encryption   = excluded.encryption,
            manufacturer = excluded.manufacturer,
            channel      = excluded.channel,
            frequency    = excluded.frequency,
            last_seen    = excluded.last_seen
        """,
        {
            "bssid":        data["bssid"],
            "ssid":         data.get("ssid"),
            "encryption":   data.get("encryption"),
            "manufacturer": data.get("manufacturer"),
            "channel":      data.get("channel"),
            "frequency":    data.get("frequency"),
            "now":          now,
        },
    )
    logger.debug("upsert_network: %s (%s)", data["bssid"], data.get("ssid"))


def insert_observation(conn: sqlite3.Connection, data: dict) -> int:
    """Добавляет наблюдение (одиночный beacon/probe) в таблицу ``observations``.

    Если координаты не переданы, ``lat``/``lon`` записываются как NULL,
    а ``has_gps`` = 0.  Транзакцию **не фиксирует**.

    Args:
        conn: Активное соединение с БД.
        data: Словарь с полями наблюдения:

            - ``bssid``     (str, обязательный) — MAC точки доступа;
            - ``rssi``      (int, опциональный) — уровень сигнала в dBm;
            - ``channel``   (int, опциональный);
            - ``frequency`` (float, опциональный);
            - ``lat``       (float, опциональный) — широта WGS-84;
            - ``lon``       (float, опциональный) — долгота WGS-84;
            - ``timestamp`` (str, опциональный)  — ISO 8601; если не задан,
              используется текущее время UTC.

    Returns:
        ``rowid`` вставленной строки (``lastrowid`` курсора).
    """
    lat = data.get("lat")
    lon = data.get("lon")
    has_gps = 1 if (lat is not None and lon is not None) else 0

    cur = conn.execute(
        """
        INSERT INTO observations
            (bssid, timestamp, lat, lon, rssi, channel, frequency, has_gps)
        VALUES
            (:bssid, :timestamp, :lat, :lon, :rssi, :channel, :frequency, :has_gps)
        """,
        {
            "bssid":     data["bssid"],
            "timestamp": data.get("timestamp") or _now_iso(),
            "lat":       lat,
            "lon":       lon,
            "rssi":      data.get("rssi"),
            "channel":   data.get("channel"),
            "frequency": data.get("frequency"),
            "has_gps":   has_gps,
        },
    )
    logger.debug(
        "insert_observation: bssid=%s rssi=%s gps=%s",
        data["bssid"], data.get("rssi"), has_gps,
    )
    return cur.lastrowid


def insert_ap_health(conn: sqlite3.Connection, data: dict) -> int:
    """Записывает результат проверки точки доступа оператора в ``ap_health``.

    Транзакцию **не фиксирует**.

    Args:
        conn: Активное соединение с БД.
        data: Словарь с полями проверки:

            - ``ap_id``     (str, обязательный) — человекочитаемый ID точки
              (например ``"ap-garage-01"``);
            - ``status``    (str, обязательный) — результат проверки:
              ``ok`` / ``no_assoc`` / ``no_dhcp`` / ``no_inet`` / ``timeout``;
            - ``rtt_ms``    (float, опциональный) — RTT в мс; NULL если
              до HTTP-проверки не дошли;
            - ``lat``       (float, опциональный);
            - ``lon``       (float, опциональный);
            - ``timestamp`` (str, опциональный) — ISO 8601; текущее UTC если
              не задан.

    Returns:
        ``rowid`` вставленной строки (``lastrowid`` курсора).
    """
    lat = data.get("lat")
    lon = data.get("lon")
    has_gps = 1 if (lat is not None and lon is not None) else 0

    cur = conn.execute(
        """
        INSERT INTO ap_health (ap_id, timestamp, status, rtt_ms, lat, lon, has_gps)
        VALUES (:ap_id, :timestamp, :status, :rtt_ms, :lat, :lon, :has_gps)
        """,
        {
            "ap_id":     data["ap_id"],
            "timestamp": data.get("timestamp") or _now_iso(),
            "status":    data["status"],
            "rtt_ms":    data.get("rtt_ms"),
            "lat":       lat,
            "lon":       lon,
            "has_gps":   has_gps,
        },
    )
    logger.debug(
        "insert_ap_health: ap_id=%s status=%s rtt_ms=%s",
        data["ap_id"], data["status"], data.get("rtt_ms"),
    )
    return cur.lastrowid


def checkpoint(conn: sqlite3.Connection) -> None:
    """Запускает WAL-checkpoint в режиме PASSIVE.

    PASSIVE означает, что checkpoint выполняется без блокировки читателей:
    SQLite переносит завершённые WAL-фреймы в основной файл по мере
    освобождения read-транзакций.  Подходит для фонового вызова по таймеру.

    Перед запуском checkpoint фиксирует открытую транзакцию (если есть):
    ``PRAGMA wal_checkpoint`` не может выполниться, пока та же connection
    держит незавершённую write-транзакцию.

    Результат (кол-во страниц в WAL / перенесённых страниц) логируется
    на уровне DEBUG.

    Args:
        conn: Активное соединение с БД.
    """
    # Фиксируем незавершённую транзакцию на этом же соединении —
    # иначе PRAGMA wal_checkpoint бросает "database table is locked".
    conn.commit()

    cur = conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    row = cur.fetchone()
    # row: (busy, log, checkpointed)
    if row:
        busy, log_pages, ckpt_pages = row[0], row[1], row[2]
        logger.debug(
            "WAL checkpoint(PASSIVE): busy=%s, log_pages=%s, checkpointed=%s",
            busy, log_pages, ckpt_pages,
        )
    else:
        logger.debug("WAL checkpoint(PASSIVE): нет данных от PRAGMA")


# ---------------------------------------------------------------------------
# Демонстрационный запуск
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    DB_PATH = "/tmp/test_wifi.db"
    logger.info("Инициализируем тестовую БД: %s", DB_PATH)
    conn = init_db(DB_PATH)

    # --- networks + observations ---
    logger.info("Вставляем тестовую сеть (первый раз)...")
    upsert_network(conn, {
        "bssid":        "AA:BB:CC:DD:EE:FF",
        "ssid":         "TestNet",
        "encryption":   "WPA2",
        "manufacturer": "Alfa",
        "channel":      6,
        "frequency":    2.437,
    })
    conn.commit()

    logger.info("Обновляем ту же сеть (second upsert — first_seen должен сохраниться)...")
    upsert_network(conn, {
        "bssid":      "AA:BB:CC:DD:EE:FF",
        "ssid":       "TestNet-renamed",
        "encryption": "WPA3",
        "channel":    11,
        "frequency":  2.462,
    })
    conn.commit()

    logger.info("Вставляем наблюдение с GPS...")
    insert_observation(conn, {
        "bssid":     "AA:BB:CC:DD:EE:FF",
        "rssi":      -67,
        "channel":   6,
        "frequency": 2.437,
        "lat":       47.2225,
        "lon":       39.7188,
    })

    logger.info("Вставляем наблюдение без GPS...")
    insert_observation(conn, {
        "bssid":     "AA:BB:CC:DD:EE:FF",
        "rssi":      -75,
        "channel":   6,
        "frequency": 2.437,
    })
    conn.commit()

    # --- ap_health ---
    logger.info("Вставляем результат проверки точки оператора...")
    insert_ap_health(conn, {
        "ap_id":  "ap-garage-01",
        "status": "ok",
        "rtt_ms": 23.5,
        "lat":    47.2225,
        "lon":    39.7188,
    })
    insert_ap_health(conn, {
        "ap_id":  "ap-garage-01",
        "status": "no_inet",
    })
    conn.commit()

    # --- checkpoint ---
    checkpoint(conn)

    # --- Вывод содержимого ---
    print("\n── networks ─────────────────────────────────────────────────────")
    for row in conn.execute("SELECT * FROM networks"):
        pprint.pprint(dict(row))

    print("\n── observations ─────────────────────────────────────────────────")
    for row in conn.execute("SELECT * FROM observations"):
        pprint.pprint(dict(row))

    print("\n── ap_health ────────────────────────────────────────────────────")
    for row in conn.execute("SELECT * FROM ap_health"):
        pprint.pprint(dict(row))

    conn.close()
    logger.info("Готово. Файл БД: %s", DB_PATH)