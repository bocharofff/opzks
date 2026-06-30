#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
src/cli.py — оркестрация всего проекта wifi-monitor.

Связывает воедино все модули: выбор Wi-Fi адаптеров и назначение ролей,
проверку GPS-фикса, три режима работы, пассивный сбор через Kismet,
проверку точек доступа оператора и экспорт данных.

Режимы (раздел 6 ТЗ):
    1 — Мониторинг               : 1 карта (monitor), Kismet + gpsd → база наблюдений
    2 — Проверка точек оператора : 1 карта (managed), обход точек, проверка интернета
    3 — Мониторинг + проверка    : 2 карты (monitor + managed), параллельно

Запуск (нужен root для работы с интерфейсами и Kismet):
    sudo python3 -m src.cli                       # интерактивный режим (меню)
    sudo python3 -m src.cli --list                # только список адаптеров
    sudo python3 -m src.cli --mode 1 --monitor AA:BB:CC:DD:EE:FF --duration 600
    sudo python3 -m src.cli --mode 2 --client wlan1 --export ap_status
    sudo python3 -m src.cli --mode 3 --monitor AA:BB:CC:DD:EE:FF --client wlan1 \
                            --duration 1800 --export heatmap wigle ap_status

Семантика --duration:
    режимы 1 и 3 : 0  → работать до Ctrl+C;  N>0 → работать N секунд
    режим 2      : 0  → один проход по точкам; N>0 → повторять проходы N секунд
"""

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

from src.config import load_config, setup_logging, validate_targets_permissions
from src import adapters as adapters_mod
from src import interface_manager as ifmgr
from src.gps_monitor import GPSMonitor
from src import kismet_runner
from src.ap_checker import APChecker
from src.db import init_db, insert_ap_health, checkpoint
from src import exporter

logger = logging.getLogger(__name__)

# Путь к кэшу выбранных ролей (раздел 7 ТЗ: «выбор может запоминаться»)
ROLE_CACHE_PATH = "data/roles.json"
# Каталог для CSV-экспорта
EXPORT_DIR = "export"
# Префикс журналов Kismet (совпадает с дефолтом kismet_runner)
KISMET_TITLE = "scan_wifi"
# Доступные профили экспорта (раздел 13 ТЗ)
EXPORT_PROFILES = ("heatmap", "full", "wigle", "ap_status")


# ===========================================================================
# Мелкие утилиты
# ===========================================================================

def _is_interactive() -> bool:
    """True, если есть интерактивный терминал (можно спрашивать пользователя)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _confirm(prompt: str) -> bool:
    """Запрашивает подтверждение y/N. При EOF/ошибке возвращает False."""
    try:
        ans = input("{} [y/N]: ".format(prompt)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "д", "да")


def _sleep_responsive(stop_event: threading.Event, seconds: float) -> None:
    """Спит до `seconds`, но просыпается сразу при установке stop_event."""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if stop_event.is_set():
            return
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))


def _parse_channels(value):
    """Парсит '1,6,11,36' → [1,6,11,36]. None/ошибка → None (авто-hopping)."""
    if not value:
        return None
    try:
        channels = [int(x.strip()) for x in value.split(",") if x.strip()]
        return channels or None
    except ValueError:
        logger.warning("Не удалось разобрать --channels=%r, используем авто-hopping", value)
        return None


def _log_dir(config: dict) -> str:
    """Каталог для журналов Kismet и базы — из db_path (по умолчанию data/)."""
    directory = os.path.dirname(config.get("db_path", "data/wifi_monitor.db")) or "data"
    return directory


def _open_db(path: str) -> sqlite3.Connection:
    """init_db + busy_timeout. busy_timeout нужен для режима 3 (две связи к WAL)."""
    conn = init_db(path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as exc:
        logger.debug("Не удалось задать busy_timeout: %s", exc)
    return conn


# ===========================================================================
# Кэш выбора ролей
# ===========================================================================

def load_role_cache() -> dict:
    """Читает data/roles.json (если есть). Никогда не бросает исключений."""
    try:
        with open(ROLE_CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_role_cache(roles: dict) -> None:
    """Сохраняет выбор ролей по MAC. Best-effort."""
    try:
        os.makedirs(os.path.dirname(ROLE_CACHE_PATH) or ".", exist_ok=True)
        with open(ROLE_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(roles, fh, ensure_ascii=False, indent=2)
        logger.info("Выбор карт сохранён: %s", ROLE_CACHE_PATH)
    except OSError as exc:
        logger.warning("Не удалось сохранить выбор карт: %s", exc)


# ===========================================================================
# Выбор адаптеров и ролей (раздел 7 ТЗ)
# ===========================================================================

def resolve_adapter(identifier: str, adapters: list) -> dict:
    """Находит адаптер по стабильному идентификатору: iface, MAC или USB-путь.

    Сначала пробует find_by_mac_or_iface (iface/MAC), затем сверяет usb_path.
    Возвращает словарь адаптера или None.
    """
    found = adapters_mod.find_by_mac_or_iface(identifier, adapters)
    if found:
        return found
    needle = identifier.strip().lower()
    for adapter in adapters:
        if (adapter.get("usb_path") or "").lower() == needle:
            return adapter
    return None


def _interactive_pick(adapters, role_desc, require_monitor, args, exclude=None):
    """Интерактивный выбор карты под роль. В неинтерактивном режиме бросает RuntimeError."""
    if args.yes or not _is_interactive():
        raise RuntimeError(
            "Не задан адаптер для роли «{}» и нет интерактивного терминала "
            "(укажите --monitor / --client по MAC, имени или USB-пути)".format(role_desc)
        )

    print("\nВыбор карты для роли: {}".format(role_desc))
    print(adapters_mod.format_adapters_table(adapters))

    while True:
        try:
            choice = input("Номер карты (1..{}): ".format(len(adapters))).strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Ввод прерван")

        if not choice.isdigit():
            print("Введите номер.")
            continue
        idx = int(choice)
        if not (1 <= idx <= len(adapters)):
            print("Номер вне диапазона.")
            continue

        adapter = adapters[idx - 1]
        if exclude is not None and adapter["iface"] == exclude["iface"]:
            print("Эта карта уже выбрана для другой роли — выберите другую.")
            continue
        if require_monitor and not adapter.get("supports_monitor"):
            if not _confirm("Карта {} не заявляет monitor mode. Всё равно выбрать?".format(adapter["iface"])):
                continue
        return adapter


def select_adapters(mode: int, args, adapters: list):
    """Назначает карты ролям монитор/клиент для выбранного режима.

    Источники выбора по приоритету: аргументы --monitor/--client → кэш ролей →
    интерактивное меню. Привязка — по стабильному идентификатору (раздел 7 ТЗ).

    Возвращает кортеж (monitor_adapter|None, client_adapter|None).
    Бросает RuntimeError, если карту под обязательную роль выбрать не удалось.
    """
    need_monitor = mode in (1, 3)
    need_client = mode in (2, 3)
    monitor_ad = None
    client_ad = None

    # Кэш используем только если оба идентификатора не заданы явно
    cache = load_role_cache() if (not args.monitor and not args.client) else {}

    if need_monitor:
        ident = args.monitor or cache.get("monitor")
        if ident:
            monitor_ad = resolve_adapter(ident, adapters)
            if not monitor_ad:
                logger.warning("Карта для роли «монитор» по '%s' не найдена", ident)
        if not monitor_ad:
            monitor_ad = _interactive_pick(
                adapters, "монитор (захват эфира)", require_monitor=True, args=args
            )

    if need_client:
        ident = args.client or cache.get("client")
        if ident:
            client_ad = resolve_adapter(ident, adapters)
            if not client_ad:
                logger.warning("Карта для роли «клиент» по '%s' не найдена", ident)
        if not client_ad:
            client_ad = _interactive_pick(
                adapters, "клиент (подключение к точкам)",
                require_monitor=False, args=args, exclude=monitor_ad,
            )

    # Режим 3 требует две РАЗНЫЕ карты (monitor и managed нельзя совместить)
    if mode == 3 and monitor_ad and client_ad and monitor_ad["iface"] == client_ad["iface"]:
        raise RuntimeError(
            "Для режима 3 нужны две разные карты: совместить monitor и managed "
            "на одном адаптере физически нельзя"
        )

    return monitor_ad, client_ad


def _prompt_mode(args):
    """Интерактивный выбор режима. В неинтерактивном режиме возвращает None."""
    if args.yes or not _is_interactive():
        logger.error("Режим не задан (--mode 1|2|3) и нет интерактивного терминала")
        return None

    print("\nРежимы работы:")
    print("  1 — Мониторинг (Kismet + GPS), 1 карта (monitor)")
    print("  2 — Проверка точек оператора, 1 карта (managed)")
    print("  3 — Мониторинг + проверка, 2 карты (monitor + managed)")
    print("  0 — Выход из программы")  # Добавили пункт выхода
    while True:
        try:
            choice = input("Выберите режим [1/2/3/0]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0  # При Ctrl+C в меню сразу возвращаем 0 на выход
        
        if choice in ("1", "2", "3"):
            return int(choice)
        if choice == "0":
            return 0  # Возвращаем 0 для корректного выхода из цикла main
        print("Введите 1, 2, 3 или 0.")


# ===========================================================================
# Подсистема GPS (раздел 8 ТЗ)
# ===========================================================================

def setup_gps(config: dict, use_gps: bool):
    """Проверяет GPS-фикс и запускает фоновый опрос. None при --no-gps."""
    if not use_gps:
        logger.info("GPS отключён (--no-gps): записи будут без координат")
        return None

    host = config.get("gps_host", "127.0.0.1")
    port = int(config.get("gps_port", 2947))
    gps = GPSMonitor(host=host, port=port)

    logger.info("Проверяем наличие GPS-фикса (gpsd %s:%s) ...", host, port)
    if gps.check_fix():
        logger.info("GPS-фикс получен")
    else:
        logger.warning(
            "GPS-фикс не получен — продолжаем без геопривязки. "
            "Наблюдения будут помечены как не имеющие координат (has_gps=0)."
        )

    gps.start()  # фоновый поток; если фикс появится позже — координаты подхватятся
    return gps


# ===========================================================================
# Режим 1 / поток мониторинга: пассивный сбор через Kismet
# ===========================================================================

def monitor_collect(conn, gps, monitor_iface, channels, log_dir, title,
                    sync_interval, stop_event, duration):
    """Полный жизненный цикл пассивного сбора на одной карте.

    Шаги: монитор-карта → unmanaged (NetworkManager) → monitor mode →
    запуск Kismet → периодическая синхронизация .kismet в проектную БД →
    остановка Kismet → финальная синхронизация → WAL checkpoint →
    восстановление интерфейса. Все ошибки логируются; интерфейс всегда
    восстанавливается в блоке finally.

    duration: число секунд или None (работать до stop_event).
    """
    orig_iface = monitor_iface
    mon_iface = None
    proc = None

    try:
        # Раздел 7 ТЗ: монитор-карта выводится из-под NetworkManager
        logger.info("Монитор-карта %s → unmanaged (NetworkManager)", orig_iface)
        ifmgr.set_unmanaged_networkmanager(orig_iface)

        logger.info("Включаем monitor mode на %s ...", orig_iface)
        mon_iface = ifmgr.set_monitor_mode(orig_iface)  # может бросить RuntimeError
        logger.info("Monitor-интерфейс: %s", mon_iface)

        logger.info("Запускаем Kismet (каналы: %s) ...", channels or "")
        proc, kismet_db = kismet_runner.start_kismet(
            mon_iface, log_dir=log_dir, title=title, channels=channels
        )
        logger.info("Kismet работает, база: %s", kismet_db)

        last_sync = 0.0  # unix-время предыдущей синхронизации (для WHERE last_time > ?)
        start = time.monotonic()

        while not stop_event.is_set():
            _sleep_responsive(stop_event, sync_interval)
            if stop_event.is_set():
                logger.info("\nСбор данных прерван оператором. Идет остановка процессов...")
                break

            now = time.time()
            try:
                count = kismet_runner.sync_kismet_to_db(kismet_db, conn, gps, since_ts=last_sync)
                logger.info("Синхронизировано наблюдений: %d", count)
            except Exception as exc:
                logger.error("Ошибка синхронизации Kismet → БД: %s", exc)
            last_sync = now
            if duration is not None and (time.monotonic() - start) >= duration:
                logger.info("Истекло заданное время мониторинга (%d c)", duration)
                break

        # Останавливаем Kismet — он сбрасывает данные на диск — и дочитываем хвост
        logger.info("Останавливаем Kismet и делаем финальную синхронизацию ...")
        kismet_runner.stop_kismet(proc)
        proc = None
        try:
            count = kismet_runner.sync_kismet_to_db(kismet_db, conn, gps, since_ts=last_sync)
            logger.info("Финальная синхронизация: %d наблюдений", count)
        except Exception as exc:
            logger.error("Ошибка финальной синхронизации: %s", exc)
        try:
            checkpoint(conn)
        except Exception as exc:
            logger.error("Ошибка WAL checkpoint: %s", exc)

    except RuntimeError as exc:
        # Например, не удалось включить monitor mode или запустить Kismet —
        # по разделу 7 ТЗ это не должно ронять процесс
        logger.error("Мониторинг не запущен: %s", exc)
    except Exception as exc:
        logger.error("Непредвиденная ошибка в мониторинге: %s", exc)
    finally:
        if proc is not None:
            kismet_runner.stop_kismet(proc)
        if mon_iface is not None:
            logger.info("Возвращаем %s в managed mode", mon_iface)
            ifmgr.set_managed_mode(mon_iface)
        logger.info("Возвращаем управление NetworkManager для %s", orig_iface)
        ifmgr.restore_networkmanager(orig_iface)


# ===========================================================================
# Режим 2 / поток проверки точек оператора
# ===========================================================================

def ap_check_loop(checker, conn, stop_event, single_pass, duration, pause):
    """Проход(ы) проверки точек оператора с записью в ap_health.

    Между точками и между проходами проверяется stop_event, поэтому остановка
    (Ctrl+C / завершение режима 3) отрабатывает корректно. Текущая проверка
    одной точки не прерывается на полпути.

    single_pass=True  — один проход (режим 2 без --duration).
    duration=N        — повторять проходы N секунд (None = до stop_event).
    """
    targets = checker.load_targets()
    if not targets:
        logger.warning("Нет включённых точек оператора для проверки")
        return

    start = time.monotonic()
    pass_no = 0
    while not stop_event.is_set():
        pass_no += 1
        logger.info("=== Проверка точек: проход #%d (%d точек) ===", pass_no, len(targets))

        for ap in targets:
            if stop_event.is_set():
                break
            ap_id = ap.get("id", "unknown")
            logger.info("── Проверка точки %s (ssid=%s) ──", ap_id, ap.get("ssid"))
            try:
                result = checker.check_one(ap)
            except Exception as exc:
                logger.error("[%s] Ошибка проверки: %s", ap_id, exc)
                continue
            try:
                insert_ap_health(conn, result)
                conn.commit()
            except Exception as exc:
                logger.error("[%s] Ошибка записи в ap_health: %s", ap_id, exc)
            rtt = "{:.0f} мс".format(result["rtt_ms"]) if result.get("rtt_ms") else "—"
            logger.info("[%s] Результат: %s, RTT: %s", ap_id, result["status"], rtt)

        if single_pass:
            break
        if duration is not None and (time.monotonic() - start) >= duration:
            logger.info("Истекло время проверки точек (%d c)", duration)
            break
        logger.info("Пауза %d c перед следующим проходом ...", pause)
        _sleep_responsive(stop_event, pause)


# ===========================================================================
# Диспетчеры режимов
# ===========================================================================

def _print_scan_results(db_path: str) -> None:
    """Читает финальные данные из проектной СУБД и выводит красивую таблицу."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Выбираем агрегированные данные по сетям и их последним наблюдениям
        rows = conn.execute("""
            SELECT n.bssid, n.ssid, n.encryption, n.channel, 
                   o.lat, o.lon, max(o.timestamp) as last_seen
            FROM networks n
            LEFT JOIN observations o ON n.bssid = o.bssid
            GROUP BY n.bssid
            ORDER BY last_seen DESC
        """).fetchall()
        
        conn.close()
        
        if not rows:
            logger.warning("После остановки мониторинга в базе данных не обнаружено записей.")
            return

        print("\n" + "=" * 94)
        print(" РЕЗУЛЬТАТЫ СКАНИРОВАНИЯ (Всего обнаружено уникальных сетей: {})".format(len(rows)))
        print("=" * 94)
        print("{:<20} {:<32} {:<10} {:<8} {:<11} {:<11}".format(
            "BSSID", "SSID", "Шифр.", "Канал", "Широта", "Долгота"
        ))
        print("-" * 94)
        for r in rows:
            print("{:<20} {:<32} {:<10} {:<8} {:<11} {:<11}".format(
                r["bssid"] or "",
                (r["ssid"] or "")[:31],
                (r["encryption"] or "")[:9],
                str(r["channel"] or ""),
                str(round(r["lat"], 5)) if r["lat"] else "No GPS",
                str(round(r["lon"], 5)) if r["lon"] else "No GPS",
            ))
        print("=" * 94 + "\n")
        
    except Exception as exc:
        logger.error("Не удалось отобразить результаты сканирования: %s", exc)


def run_mode_1(config, monitor_ad, gps, stop_event, duration, channels):
    """Режим 1 — только мониторинг (одна карта в monitor mode) с выводом результатов."""
    conn = _open_db(config["db_path"])
    try:
        monitor_collect(
            conn, gps, monitor_ad["iface"], channels,
            _log_dir(config), KISMET_TITLE, int(config["sync_interval_sec"]),
            stop_event, duration if duration > 0 else None,
        )
    finally:
        # 2. Закрываем соединение с базой (вызовется и при Ctrl+C, и при штатном выходе)
        conn.close()

    # 3. Теперь отчет выведется железно в любом сценарии
    logger.info("Формирование отчета по собранным данным...")
    _print_scan_results(config["db_path"])


def run_mode_2(config, client_ad, gps, stop_event, duration, pause):
    """Режим 2 — только проверка точек оператора (одна карта в managed mode).

    Клиентская карта остаётся под обычным управлением; APChecker сам
    запускает wpa_supplicant под каждую точку и убирает за собой.
    """
    validate_targets_permissions(config["targets_path"])
    conn = _open_db(config["db_path"])
    try:
        checker = APChecker(
            iface=client_ad["iface"],
            targets_path=config["targets_path"],
            conn=conn,
            gps=gps,
        )
        ap_check_loop(
            checker, conn, stop_event,
            single_pass=(duration == 0),
            duration=(duration if duration > 0 else None),
            pause=pause,
        )
    finally:
        conn.close()


def run_mode_3(config, monitor_ad, client_ad, gps, stop_event, duration, channels, pause):
    """Режим 3 — мониторинг и проверка точек параллельно на двух картах.

    Раздел 7 ТЗ: NetworkManager не отключается полностью — только монитор-карта
    переводится в unmanaged (это делает monitor_collect), клиентская карта
    остаётся управляемой.

    Каждый поток работает со своей связью к БД (WAL + busy_timeout допускают
    одновременную запись), GPS-объект общий и потокобезопасный.
    """
    validate_targets_permissions(config["targets_path"])
    conn_mon = _open_db(config["db_path"])
    conn_ap = _open_db(config["db_path"])

    mon_thread = threading.Thread(
        target=monitor_collect,
        name="monitor",
        args=(conn_mon, gps, monitor_ad["iface"], channels,
              _log_dir(config), KISMET_TITLE, int(config["sync_interval_sec"]),
              stop_event, None),
        daemon=True,
    )
    checker = APChecker(
        iface=client_ad["iface"],
        targets_path=config["targets_path"],
        conn=conn_ap,
        gps=gps,
    )
    ap_thread = threading.Thread(
        target=ap_check_loop,
        name="ap-check",
        args=(checker, conn_ap, stop_event, False, None, pause),
        daemon=True,
    )

    logger.info(
        "Режим 3: мониторинг (%s) + проверка точек (%s) параллельно",
        monitor_ad["iface"], client_ad["iface"],
    )
    logger.info("NetworkManager НЕ отключается: монитор → unmanaged, клиент остаётся управляемым")

    mon_thread.start()
    ap_thread.start()

    # Главный поток ждёт окончания заданного времени или сигнала
    try:
        if duration and duration > 0:
            end = time.monotonic() + duration
            while time.monotonic() < end and not stop_event.is_set():
                time.sleep(0.5)
        else:
            while not stop_event.is_set():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()

    logger.info("Останавливаем потоки ...")
    mon_thread.join(timeout=60)
    ap_thread.join(timeout=90)  # текущая проверка точки может длиться до ~70 c
    if mon_thread.is_alive():
        logger.warning("Поток мониторинга не завершился вовремя")
    if ap_thread.is_alive():
        logger.warning("Поток проверки точек не завершился вовремя")

    conn_mon.close()
    conn_ap.close()


# ===========================================================================
# Экспорт (раздел 13 ТЗ)
# ===========================================================================

def do_exports(db_path, profiles, bssid_filter):
    """Выгружает указанные профили в export/<профиль>_<timestamp>.csv."""
    if not os.path.exists(db_path):
        logger.error("База не найдена для экспорта: %s", db_path)
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs(EXPORT_DIR, exist_ok=True)

    handlers = {
        "heatmap":   lambda c, out: exporter.export_heatmap_csv(c, out, bssid_filter=bssid_filter),
        "full":      exporter.export_full_dataset_csv,
        "wigle":     exporter.export_wigle_csv,
        "ap_status": exporter.export_ap_status_csv,
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for profile in profiles:
            handler = handlers.get(profile)
            if handler is None:
                logger.warning("Неизвестный профиль экспорта: %s", profile)
                continue
            out_path = os.path.join(EXPORT_DIR, "{}_{}.csv".format(profile, ts))
            try:
                count = handler(conn, out_path)
                logger.info("Экспорт «%s»: %s строк → %s", profile, count, out_path)
            except Exception as exc:
                logger.error("Ошибка экспорта «%s»: %s", profile, exc)
    finally:
        conn.close()


# ===========================================================================
# Разбор аргументов
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="wifi-monitor — оркестратор: мониторинг Wi-Fi с геопривязкой "
                    "и контроль точек доступа оператора.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
режимы (--mode):
  1  мониторинг (Kismet + GPS), 1 карта в monitor mode
  2  проверка точек оператора, 1 карта в managed mode
  3  оба режима одновременно, 2 разные карты

--duration:
  режимы 1 и 3 : 0 -> до Ctrl+C; N>0 -> N секунд
  режим 2      : 0 -> один проход; N>0 -> повторять проходы N секунд

примеры:
  sudo python -m src.cli --list
  sudo python -m src.cli                # интерактивное меню
  sudo python -m src.cli --mode 1 --monitor AA:BB:CC:DD:EE:FF --duration 600 --export heatmap wigle
  sudo python -m src.cli --mode 2 --client wlan1 --export ap_status
  sudo python -m src.cli --mode 3 --monitor AA:BB:CC:DD:EE:FF --client wlan1 --duration 1800
""",
    )

    parser.add_argument("--config", default="config/settings.yaml", metavar="<путь>",
                        help="путь к settings.yaml (default: config/settings.yaml)")
    parser.add_argument("--mode", type=int, choices=(1, 2, 3), metavar="{1,2,3}",
                        help="режим работы; без него — интерактивное меню")
    parser.add_argument("--monitor", metavar="<MAC|iface|usb>",
                        help="карта для роли монитор (стабильный идентификатор)")
    parser.add_argument("--client", metavar="<MAC|iface|usb>",
                        help="карта для роли клиент (стабильный идентификатор)")
    parser.add_argument("--channels", metavar="1,6,11,36",
                        help="список каналов для Kismet; без него — авто-hopping")
    parser.add_argument("--duration", type=int, default=0, metavar="<сек>",
                        help="длительность работы; см. справку (default: 0)")
    parser.add_argument("--ap-pause", type=int, default=5, metavar="<сек>",
                        dest="ap_pause",
                        help="пауза между проходами проверки точек (default: 5)")
    parser.add_argument("--list", action="store_true",
                        help="вывести список Wi-Fi адаптеров и выйти")
    parser.add_argument("--no-gps", action="store_true", dest="no_gps",
                        help="не использовать GPS (записи без координат)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="неинтерактивный режим (не задавать вопросов)")
    parser.add_argument("--export", nargs="+", choices=EXPORT_PROFILES, metavar="ПРОФИЛЬ",
                        help="экспортировать профили после работы: "
                             + " | ".join(EXPORT_PROFILES))
    parser.add_argument("--export-bssid", dest="export_bssid", metavar="AA:BB:CC:DD:EE:FF",
                        help="фильтр по BSSID для профиля heatmap")

    return parser


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(args.config)
    setup_logging(config)

    logger.info("=== wifi-monitor — оркестратор ===")

    adapters = adapters_mod.list_wifi_adapters()

    if args.list:
        print(adapters_mod.format_adapters_table(adapters))
        return 0

    if not adapters:
        logger.error("Wi-Fi адаптеры не найдены. Проверьте проброс USB-устройств в ВМ.")
        return 1

    # GPS инициализируется один раз на всё время работы программы
    gps = setup_gps(config, use_gps=not args.no_gps)

    channels = _parse_channels(args.channels)

    try:
        # Интерактивный цикл: крутится, пока пользователь сам не выйдет из меню
        while True:
            # Сбрасываем флаг остановки перед запуском нового режима
            mode = args.mode
            if mode is None:
                try:
                    mode = _prompt_mode(args)
                except (KeyboardInterrupt, EOFError):
                    print("") # Снос строки после ^C в меню
                    break # Выход из программы, если нажали Ctrl+C прямо в меню
                if mode is None:
                    break
            
            try:
                monitor_ad, client_ad = select_adapters(mode, args, adapters)
            except RuntimeError as exc:
                logger.error("%s", exc)
                return 2
                
            # Лог выбранных ролей по стабильному идентификатору
            if monitor_ad:
                logger.info("Роль МОНИТОР: %s (MAC %s, USB %s, чипсет %s)",
                            monitor_ad["iface"], monitor_ad["mac"],
                            monitor_ad.get("usb_path"), monitor_ad.get("chipset"))
                if not monitor_ad.get("supports_monitor"):
                    logger.warning(
                        "Карта %s не заявляет поддержку monitor mode — "
                        "Kismet может не запуститься", monitor_ad["iface"]
                    )
                    if _is_interactive() and not args.yes and not _confirm("Продолжить всё равно?"):
                        logger.info("Отменено пользователем")
                        return 0
            if client_ad:
                logger.info("Роль КЛИЕНТ: %s (MAC %s, USB %s)", client_ad["iface"], client_ad["mac"], client_ad.get("usb_path"))

            run_mode(config, monitor_ad, client_ad, gps, args.duration, channels, args.ap_pause, mode)
            
            # Если режим жестко задан аргументом командной строки (например, --mode 1) — выходим
            if args.mode is not None:
                break

            print("\nВозврат в главное меню выбор режима...")
            time.sleep(1)

    finally:
        if gps is not None:
            gps.stop()

    logger.info("Готово.")
    return 0

def run_mode(config, monitor_ad, client_ad, gps, duration, channels, ap_pause,  mode):
    # Событие stop_event теперь сбрасываемое, оно сигнализирует об остановке ТЕКУЩЕГО режима
    stop_event = threading.Event()
    def _handle_signal(signum, _frame):
        logger.info("Получен сигнал %s — прерываем текущий режим ...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if mode == 1:
        run_mode_1(config, monitor_ad, gps, stop_event, duration, channels)
    elif mode == 2:
        run_mode_2(config, client_ad, gps, stop_event, duration, ap_pause)
    elif mode == 3:
        run_mode_3(config, monitor_ad, client_ad, gps, stop_event, duration, channels, ap_pause)
            

if __name__ == "__main__":
    sys.exit(main())