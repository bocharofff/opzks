# -*- coding: utf-8 -*-
"""
src/ap_checker.py

Проверяет работоспособность точек доступа оператора:
ассоциируется, получает IP через DHCP, проверяет выход в интернет.

Используется из src/cli.py (режимы 2 и 3).
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
    "dhcp_timeout_s":  20,
}


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
            auth_block = "\tkey_mgmt=WPA-PSK\n\tproto=RSN\n\tpairwise=CCMP\n"
        elif security == "wpa3-sae":
            auth_block = "\tkey_mgmt=SAE\n\tieee80211w=2\n"
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

        conf = (
            "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
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
    # Проверка одной точки
    # ------------------------------------------------------------------

    def check_one(self, ap):
        """
        Полный цикл проверки одной точки.

        Возвращает dict: {ap_id, status, rtt_ms, lat, lon, timestamp}.
        psk/psk_hash НИКОГДА не включаются в результат.
        """
        ap_id           = ap.get("id", "unknown")
        dhcp_timeout_s  = int(ap.get("dhcp_timeout_s",  self._defaults["dhcp_timeout_s"]))
        check_timeout_s = int(ap.get("check_timeout_s", self._defaults["check_timeout_s"]))
        check_url       = ap.get("check_url", self._defaults["check_url"])

        status   = "error"
        rtt_ms   = None
        conf_path = None

        try:
            # ----------------------------------------------------------
            # Шаг 1: создаём wpa_supplicant.conf
            # ----------------------------------------------------------
            conf_path = self._write_wpa_conf(ap)

            # ----------------------------------------------------------
            # Шаг 2: запускаем wpa_supplicant в фоне (-B)
            # ----------------------------------------------------------
            rc, _, stderr = _run(
                ["wpa_supplicant", "-B", "-i", self.iface, "-c", conf_path],
                timeout=10,
            )
            if rc != 0:
                logger.warning(
                    "[%s] wpa_supplicant не запустился: %s", ap_id, stderr.strip()
                )
                status = "no_assoc"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 3: polling ассоциации
            # ----------------------------------------------------------
            associated = False
            for _ in range(dhcp_timeout_s):
                time.sleep(1)
                _, out, _ = _run(["iw", "dev", self.iface, "link"], timeout=5)
                if "Connected" in out or ("SSID" in out and "Not connected" not in out):
                    associated = True
                    logger.debug("[%s] Ассоциация подтверждена", ap_id)
                    break

            if not associated:
                logger.info("[%s] Нет ассоциации за %d сек", ap_id, dhcp_timeout_s)
                status = "no_assoc"
                return self._make_result(ap_id, status, rtt_ms)

            # ----------------------------------------------------------
            # Шаг 4: DHCP
            # ----------------------------------------------------------
            _run(["dhclient", "-1", self.iface], timeout=dhcp_timeout_s + 5)

            _, addr_out, _ = _run(["ip", "addr", "show", self.iface], timeout=5)
            if "inet " not in addr_out:
                logger.info("[%s] IP не получен (нет DHCP)", ap_id)
                status = "no_dhcp"
                return self._make_result(ap_id, status, rtt_ms)

            logger.debug("[%s] IP получен, проверяем интернет...", ap_id)

            # ----------------------------------------------------------
            # Шаг 5: HTTP-проверка
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
            self._cleanup(conf_path)

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

    def _cleanup(self, conf_path):
        """
        Останавливает wpa_supplicant, сбрасывает IP, удаляет conf.
        Все ошибки логируются, но не пробрасываются.
        """
        # Останавливаем wpa_supplicant
        rc, _, _ = _run(
            ["wpa_cli", "-i", self.iface, "terminate"], timeout=5
        )
        if rc != 0:
            # Fallback: pkill если wpa_cli не сработал
            _run(["pkill", "-f", "wpa_supplicant.*{}".format(self.iface)], timeout=5)

        # Сбрасываем IP
        _run(["ip", "addr", "flush", "dev", self.iface], timeout=5)
        # Освобождаем аренду DHCP если dhclient его держит
        _run(["dhclient", "-r", self.iface], timeout=5)

        # Удаляем временный conf (содержит пароль)
        if conf_path and os.path.exists(conf_path):
            try:
                os.unlink(conf_path)
                logger.debug("Временный conf удалён: %s", conf_path)
            except OSError as exc:
                logger.error("Не удалось удалить conf %s: %s", conf_path, exc)

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

  python -m src.ap_checker <интерфейс> <ap_targets.yaml>

  Запускает проверку всех включённых точек из файла и выводит результаты.
  БД не используется (результаты только в stdout).

Пример:
  sudo python -m src.ap_checker wlan1 config/ap_targets.yaml

Требования:
  - <интерфейс> должен быть в managed mode (не monitor)
  - wpa_supplicant, dhclient, iw должны быть установлены
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

    # Запуск без реальной БД и GPS — только вывод в stdout
    class _NullConn:
        def commit(self): pass

    class _NullGPS:
        def latest(self): return None

    # Мокаем insert_ap_health чтобы не нужна реальная БД
    import src.ap_checker as _self_mod
    _self_mod.insert_ap_health = lambda conn, data: None

    checker = APChecker(
        iface=iface,
        targets_path=targets_path,
        conn=_NullConn(),
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