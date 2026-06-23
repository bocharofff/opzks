# -*- coding: utf-8 -*-
"""
src/ap_checker.py

Проверяет работоспособность точек доступа оператора:
ассоциируется, получает IP через DHCP, проверяет выход в интернет.

Используется из src/cli.py (режимы 2 и 3).

Механизм ассоциации: wpa_supplicant (-B -P pidfile) + wpa_cli -a action-скрипт.
Action-скрипт реагирует на событие CONNECTED, поднимает интерфейс и запускает
dhclient. Скрипт поллит появление IP-адреса и nameserver в resolv.conf.
"""

import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import requests
import yaml

from src.db import insert_ap_health

logger = logging.getLogger(__name__)

# Значения по умолчанию если не заданы в ap_targets.yaml
_DEFAULTS = {
    "check_url":       "http://connectivitycheck.gstatic.com/generate_204",
    "check_timeout_s": 10,
    "dhcp_timeout_s":  30,
}

# action-скрипт для wpa_cli -a.
# wpa_cli вызывает его с аргументами: <iface> <event>.
# event = CONNECTED при успешной ассоциации, DISCONNECTED при разрыве.
_WPA_ACTION_SCRIPT = """#!/bin/bash
IFACE="$1"
CMD="$2"
logger "wpa_action: iface=$IFACE cmd=$CMD"
if [ "$CMD" = "CONNECTED" ]; then
    ip link set "$IFACE" up
    dhclient -r "$IFACE" 2>/dev/null
    dhclient -v "$IFACE"
fi
"""


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _run(cmd, timeout=15):
    """
    Запускает команду, возвращает (returncode, stdout, stderr).
    Никогда не бросает исключений наружу.
    """
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "not found: {}".format(cmd[0])
    except Exception as exc:
        return -1, "", str(exc)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class APChecker:
    """
    Проверяет точки доступа из ap_targets.yaml одну за другой.

    Использует отдельный managed-интерфейс (не тот, что у Kismet).
    Пароли никогда не попадают в логи, исключения или возвращаемые данные.
    """

    def __init__(self, iface, targets_path, conn, gps, defaults=None):
        self.iface = iface
        self.targets_path = targets_path
        self.conn = conn
        self.gps = gps
        # Объединяем встроенные дефолты с переданными
        self._defaults = dict(_DEFAULTS)
        if defaults:
            self._defaults.update(defaults)

    # ------------------------------------------------------------------
    # Загрузка конфига
    # ------------------------------------------------------------------

    def load_targets(self):
        """
        Читает ap_targets.yaml, проверяет права файла, возвращает
        список включённых точек с подставленными defaults.
        Пароли (psk/psk_hash) НИКОГДА не логируются.
        """
        # Проверка прав доступа к файлу
        try:
            mode = oct(os.stat(self.targets_path).st_mode)
            if mode != "0o100600":
                logger.warning(
                    "Небезопасные права на %s: %s (ожидается 0o100600). "
                    "Исправьте: chmod 600 %s",
                    self.targets_path, mode, self.targets_path,
                )
        except OSError as exc:
            logger.error("Не удалось проверить права %s: %s", self.targets_path, exc)

        # Чтение YAML
        try:
            with open(self.targets_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except FileNotFoundError:
            logger.error("Файл точек не найден: %s", self.targets_path)
            return []
        except yaml.YAMLError as exc:
            logger.error("Ошибка парсинга %s: %s", self.targets_path, exc)
            return []

        if not isinstance(data, dict):
            logger.error("Неверный формат %s: ожидается dict", self.targets_path)
            return []

        # Дефолты из файла имеют приоритет над встроенными, но не над полями точки
        file_defaults = dict(self._defaults)
        file_defaults.update(data.get("defaults") or {})

        targets = []
        for ap in data.get("access_points") or []:
            if not ap.get("enabled", True):
                continue
            # Объединяем: дефолты < поля точки
            merged = dict(file_defaults)
            merged.update(ap)
            targets.append(merged)

        logger.info(
            "Загружено %d включённых точек из %s",
            len(targets), self.targets_path,
        )
        return targets

    # ------------------------------------------------------------------
    # Генерация wpa_supplicant.conf
    # ------------------------------------------------------------------

    def _write_wpa_conf(self, ap):
        """
        Создаёт временный wpa_supplicant.conf для точки ap.
        Формат идентичен рабочему test_wpa2.conf.
        Устанавливает права 600. Возвращает путь к файлу.
        Содержимое файла (и пароли) НИКОГДА не логируются.
        """
        security = (ap.get("security") or "open").lower()
        ssid    = ap.get("ssid", "")
        bssid   = ap.get("bssid")
        hidden  = ap.get("hidden", False)

        # psk_hash имеет приоритет над psk (уже хешированный, без кавычек)
        psk_line = ""
        if security not in ("open",):
            if ap.get("psk_hash"):
                psk_line = "\tpsk={}\n".format(ap["psk_hash"])
            elif ap.get("psk"):
                psk_line = '\tpsk="{}"\n'.format(ap["psk"])

        # Формируем блок network={}
        if security == "open":
            auth_block = "\tkey_mgmt=NONE\n"
        elif security == "wpa2-psk":
            # group=CCMP добавлен явно — как в рабочем test_wpa2.conf
            auth_block = (
                "\tkey_mgmt=WPA-PSK\n"
                "\tproto=RSN\n"
                "\tpairwise=CCMP\n"
                "\tgroup=CCMP\n"
            )
        elif security == "wpa3-sae":
            auth_block = "\tkey_mgmt=SAE\n\tieee80211w=1\n"
        elif security == "wpa2-eap":
            auth_block = "\tkey_mgmt=WPA-EAP\n\tproto=RSN\n"
            # TODO: добавить eap= identity= password= по необходимости
        else:
            logger.warning("Неизвестный тип security '%s', использую WPA-PSK", security)
            auth_block = "\tkey_mgmt=WPA-PSK\n"

        optional = ""
        if bssid:
            optional += "\tbssid={}\n".format(bssid)
        if hidden:
            optional += "\tscan_ssid=1\n"

        # ctrl_interface и group — как в рабочем конфиге (путь + group=0,
        # а не DIR=... GROUP=netdev)
        conf = (
            "ctrl_interface=/var/run/wpa_supplicant\n"
            "ctrl_interface_group=0\n"
            "network={{\n"
            "\tssid=\"{ssid}\"\n"
            "{auth}{psk}{optional}"
            "}}\n"
        ).format(
            ssid=ssid,
            auth=auth_block,
            psk=psk_line,
            optional=optional,
        )

        fd, path = tempfile.mkstemp(suffix=".conf", prefix="wpa_ap_")
        try:
            os.write(fd, conf.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
        # Логируем только путь, НИКОГДА не содержимое
        logger.debug("wpa_supplicant.conf создан: %s (ssid=%s)", path, ssid)
        return path

    # ------------------------------------------------------------------
    # Генерация action-скрипта для wpa_cli
    # ------------------------------------------------------------------

    def _write_action_script(self):
        """
        Создаёт временный action-скрипт для wpa_cli -a.
        Права 700 (исполняемый). Возвращает путь.
        """
        fd, path = tempfile.mkstemp(suffix=".sh", prefix="wpa_action_")
        try:
            os.write(fd, _WPA_ACTION_SCRIPT.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o700)
        logger.debug("action-скрипт создан: %s", path)
        return path

    # ------------------------------------------------------------------
    # Проверка одной точки
    # ------------------------------------------------------------------

    def check_one(self, ap):
        """
        Полный цикл проверки одной точки через wpa_cli -a action-скрипт.

        wpa_supplicant запускается с PID-файлом, wpa_cli с action-скриптом
        реагирует на CONNECTED и сам запускает dhclient. Мы поллим появление
        IP-адреса как признак ассоциации + DHCP, затем готовность DNS.

        Возвращает dict: {ap_id, status, rtt_ms, lat, lon, timestamp}.
        psk/psk_hash НИКОГДА не включаются в результат.
        """
        ap_id           = ap.get("id", "unknown")
        dhcp_timeout_s  = int(ap.get("dhcp_timeout_s",  self._defaults["dhcp_timeout_s"]))
        check_timeout_s = int(ap.get("check_timeout_s", self._defaults["check_timeout_s"]))
        check_url       = ap.get("check_url", self._defaults["check_url"])

        status       = "error"
        rtt_ms       = None
        conf_path    = None
        action_path  = None
        pid_path     = None
        wpa_cli_proc = None

        try:
            # ----------------------------------------------------------
            # Шаг 1: создаём wpa_supplicant.conf и action-скрипт
            # ----------------------------------------------------------
            conf_path = self._write_wpa_conf(ap)
            action_path = self._write_action_script()
            pid_path = conf_path + ".pid"

            # ----------------------------------------------------------
            # Шаг 2: полная очистка состояния ПЕРЕД подключением,
            #        затем запуск нового wpa_supplicant (-B -P).
            #
            # Без этого dhclient на следующем прогоне спотыкается на
            # "Error: ipv4: Address already assigned" — старый IP с прошлой
            # проверки остаётся на интерфейсе, и первый DHCP-цикл сбоит,
            # из-за чего DNS встаёт с большой задержкой.
            # ----------------------------------------------------------
            # 2a. Гасим старый wpa_supplicant
            _run(["killall", "wpa_supplicant"], timeout=5)
            time.sleep(1)
            # 2b. Удаляем осиротевший ctrl_interface сокет, иначе новый
            #     процесс упадёт на "Failed to initialize control interface"
            _run(["rm", "-f", "/var/run/wpa_supplicant/{}".format(self.iface)], timeout=5)
            # 2c. Освобождаем старую DHCP-аренду (если dhclient её держит)
            _run(["dhclient", "-r", self.iface], timeout=5)
            # 2d. Гасим возможные осиротевшие dhclient на этом интерфейсе
            _run(["pkill", "-f", "dhclient.*{}".format(self.iface)], timeout=5)
            # 2e. Сбрасываем все IP-адреса с интерфейса
            _run(["ip", "addr", "flush", "dev", self.iface], timeout=5)
            # 2f. Поднимаем интерфейс (flush мог его не тронуть, но на всякий)
            _run(["ip", "link", "set", self.iface, "up"], timeout=5)

            rc, stdout, stderr = _run(
                ["wpa_supplicant", "-B", "-i", self.iface,
                 "-c", conf_path, "-P", pid_path],
                timeout=10,
            )
            if rc != 0:
                logger.warning(
                    "[%s] wpa_supplicant не запустился (rc=%d): stdout=%r stderr=%r",
                    ap_id, rc, stdout.strip(), stderr.strip(),
                )
                status = "no_assoc"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 3: запускаем wpa_cli -a в фоне (Popen, не _run).
            # Он сам поднимет интерфейс и запустит dhclient по CONNECTED.
            # ----------------------------------------------------------
            try:
                wpa_cli_proc = subprocess.Popen(
                    ["wpa_cli", "-i", self.iface, "-a", action_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.warning("[%s] не удалось запустить wpa_cli -a: %s", ap_id, exc)
                status = "error"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 4: ждём появления IP (action-скрипт уже сделал dhclient)
            # ----------------------------------------------------------
            got_ip = False
            for _ in range(dhcp_timeout_s):
                time.sleep(1)
                _, addr_out, _ = _run(["ip", "addr", "show", self.iface], timeout=5)
                if "inet " in addr_out:
                    got_ip = True
                    logger.debug("[%s] IP получен", ap_id)
                    break

            if not got_ip:
                # Различаем "не ассоциировались" и "ассоциировались, но нет DHCP"
                _, link_out, _ = _run(["iw", "dev", self.iface, "link"], timeout=5)
                if "Connected" in link_out:
                    logger.info("[%s] Ассоциация есть, но IP не получен", ap_id)
                    status = "no_dhcp"
                else:
                    logger.info("[%s] Нет ассоциации за %d сек", ap_id, dhcp_timeout_s)
                    status = "no_assoc"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 5: ждём готовности DNS — реальной попыткой резолва.
            #
            # Проверка "nameserver in resolv.conf" ненадёжна: строка есть,
            # но сервер ещё не отвечает / маршрут не готов. Резолвим сам хост
            # тем же резолвером, что потом использует requests. Окно 25с —
            # на реальной точке от ассоциации до рабочего DNS уходит ~15-20с.
            # ----------------------------------------------------------
            import socket
            from urllib.parse import urlparse

            check_host = urlparse(check_url).hostname or ""
            logger.debug("[%s] IP получен, ждём резолва %s...", ap_id, check_host)

            dns_ready = False
            for _ in range(25):
                time.sleep(1)
                try:
                    socket.gethostbyname(check_host)
                    dns_ready = True
                    logger.debug("[%s] DNS резолвит %s", ap_id, check_host)
                    break
                except socket.gaierror:
                    continue

            if not dns_ready:
                _, resolv, _ = _run(["cat", "/etc/resolv.conf"], timeout=5)
                logger.info(
                    "[%s] DNS не резолвит за 25с. resolv.conf:\n%s",
                    ap_id, resolv.strip(),
                )
                status = "no_dns"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 6: HTTP-проверка
            # ----------------------------------------------------------
            t0 = time.monotonic()
            try:
                resp = requests.get(
                    check_url,
                    timeout=check_timeout_s,
                    allow_redirects=False,
                )
                rtt_ms = (time.monotonic() - t0) * 1000
                if resp.status_code == 204:
                    status = "ok"
                else:
                    # Редирект или captive portal — интернет есть, но перехватывается
                    logger.info(
                        "[%s] Captive portal или неожиданный код: %d",
                        ap_id, resp.status_code,
                    )
                    status = "captive_portal"
            except requests.exceptions.Timeout:
                rtt_ms = (time.monotonic() - t0) * 1000
                status = "timeout"
            except requests.exceptions.RequestException as exc:
                logger.info("[%s] HTTP-запрос завершился ошибкой: %s", ap_id, exc)
                status = "no_inet"

        except Exception as exc:
            logger.error("[%s] Неожиданная ошибка при проверке: %s", ap_id, exc)
            status = "error"

        finally:
            # ----------------------------------------------------------
            # Cleanup — выполняется всегда
            # ----------------------------------------------------------
            self._cleanup(conf_path, action_path, pid_path, wpa_cli_proc)

        return self._make_result(ap_id, status, rtt_ms)

    def _make_result(self, ap_id, status, rtt_ms):
        """Формирует dict результата. Пароли никогда не включаются."""
        pos = None
        if self.gps is not None:
            try:
                pos = self.gps.latest()
            except Exception:
                pass

        return {
            "ap_id":     ap_id,
            "timestamp": _now_iso(),
            "status":    status,
            "rtt_ms":    rtt_ms,
            "lat":       pos["lat"] if pos else None,
            "lon":       pos["lon"] if pos else None,
        }

    def _cleanup(self, conf_path, action_path=None, pid_path=None, wpa_cli_proc=None):
        """
        Гасит wpa_cli -a, останавливает wpa_supplicant, сбрасывает IP,
        удаляет временные файлы. Все ошибки логируются, не пробрасываются.
        """
        # Гасим фоновый wpa_cli -a
        if wpa_cli_proc is not None:
            try:
                wpa_cli_proc.terminate()
                wpa_cli_proc.wait(timeout=5)
            except Exception:
                try:
                    wpa_cli_proc.kill()
                except Exception:
                    pass

        # Останавливаем wpa_supplicant по PID-файлу, если есть
        killed = False
        if pid_path and os.path.exists(pid_path):
            try:
                with open(pid_path) as fh:
                    pid = int(fh.read().strip())
                os.kill(pid, 15)
                killed = True
            except (OSError, ValueError) as exc:
                logger.debug("Не удалось убить по PID-файлу: %s", exc)

        if not killed:
            rc, _, _ = _run(["wpa_cli", "-i", self.iface, "terminate"], timeout=5)
            if rc != 0:
                # Fallback: pkill если wpa_cli не сработал
                _run(["pkill", "-f", "wpa_supplicant.*{}".format(self.iface)], timeout=5)

        # Освобождаем аренду DHCP и сбрасываем IP
        _run(["dhclient", "-r", self.iface], timeout=5)
        _run(["ip", "addr", "flush", "dev", self.iface], timeout=5)

        # Удаляем временные файлы (conf содержит пароль)
        for p in (conf_path, action_path, pid_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                    logger.debug("Удалён временный файл: %s", p)
                except OSError as exc:
                    logger.error("Не удалось удалить %s: %s", p, exc)

    # ------------------------------------------------------------------
    # Обход всех точек
    # ------------------------------------------------------------------

    def run_all(self):
        """
        Проверяет все включённые точки по очереди.
        Каждый результат сохраняется в ap_health.
        Возвращает список результатов (без паролей).
        """
        targets = self.load_targets()
        if not targets:
            logger.warning("Нет включённых точек для проверки")
            return []

        results = []
        for ap in targets:
            ap_id = ap.get("id", "unknown")
            logger.info("── Проверка точки: %s (ssid=%s) ──", ap_id, ap.get("ssid"))

            result = self.check_one(ap)

            # Сохраняем в БД
            try:
                insert_ap_health(self.conn, result)
                self.conn.commit()
            except Exception as exc:
                logger.error("[%s] Ошибка записи в ap_health: %s", ap_id, exc)

            # Итоговый статус в лог
            rtt_str = "{:.0f}мс".format(result["rtt_ms"]) if result["rtt_ms"] else "—"
            logger.info(
                "[%s] Результат: %s, RTT: %s",
                ap_id, result["status"], rtt_str,
            )
            results.append(result)

        return results


# ---------------------------------------------------------------------------
# Запуск из командной строки
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    USAGE = """
Использование (необходим root):

  sudo python3 -m src.ap_checker <интерфейс> <ap_targets.yaml>

  Запускает проверку всех включённых точек из файла и выводит результаты.
  БД не используется (результаты только в stdout).

Пример:
  sudo python3 -m src.ap_checker wlan0 config/ap_targets.yaml

Требования:
  - <интерфейс> должен быть в managed mode (не monitor)
  - wpa_supplicant, wpa_cli, dhclient, iw должны быть установлены
  - ap_targets.yaml должен иметь права 600
""".strip()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) < 3:
        print(USAGE)
        sys.exit(1)

    iface         = sys.argv[1]
    targets_path  = sys.argv[2]

    # Используем реальную БД в памяти — insert_ap_health работает без изменений
    import sqlite3 as _sqlite3

    _conn = _sqlite3.connect(":memory:")
    _conn.executescript("""
        CREATE TABLE ap_health (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ap_id     TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status    TEXT NOT NULL,
            rtt_ms    REAL,
            lat       REAL,
            lon       REAL,
            has_gps   INTEGER NOT NULL DEFAULT 0
        );
    """)
    _conn.commit()

    class _NullGPS:
        def latest(self): return None

    checker = APChecker(
        iface=iface,
        targets_path=targets_path,
        conn=_conn,
        gps=_NullGPS(),
    )
    results = checker.run_all()

    print("\n" + "=" * 70)
    print("РЕЗУЛЬТАТЫ ПРОВЕРКИ ТОЧЕК")
    print("=" * 70)
    print("{:<20} {:<15} {:<10}".format("AP ID", "Статус", "RTT"))
    print("-" * 70)
    for r in results:
        rtt = "{:.0f}мс".format(r["rtt_ms"]) if r["rtt_ms"] else "—"
        print("{:<20} {:<15} {:<10}".format(r["ap_id"], r["status"], rtt))
    print("=" * 70)