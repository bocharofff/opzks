# -*- coding: utf-8 -*-
"""
src/ap_checker.py

Проверяет работоспособность точек доступа оператора:
ассоциируется, получает IP через DHCP, проверяет выход в интернет.

Используется из src/cli.py (режимы 2 и 3).

Механизм ассоциации: временный connection-профиль NetworkManager
(``nmcli connection add`` → ``up`` → ``down`` → ``delete``).

Почему не отдельный wpa_supplicant (было раньше): клиентская карта намеренно
остаётся под управлением NetworkManager (раздел 7 ТЗ, критерий приёмки §17 —
«без отключения сетевого менеджера для клиентской карты»). NetworkManager сам
держит собственный процесс wpa_supplicant на managed-интерфейсе (D-Bus/systemd,
переживает killall и почти мгновенно перезапускается). Интерфейс физически не
может одновременно принадлежать двум процессам wpa_supplicant — раньше
`killall wpa_supplicant` валил supplicant NetworkManager'а, тот тут же
поднимался заново и отбирал интерфейс обратно, из-за чего проверка стабильно
падала в ``no_assoc`` (см. журнал реального прогона на Kali). Теперь ассоциацию
и DHCP выполняет сам NetworkManager — конфликта на уровне netlink/ctrl_iface
больше нет.
"""

import logging
import os
import subprocess
import time
import uuid
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

# Причины отказа NetworkManager (GENERAL.STATE-REASON из `nmcli device show`),
# связанные именно с получением IP по DHCP — всё остальное (нет секретов/
# неверный пароль, таймаут supplicant, сеть не найдена и т.п.) трактуется как
# проблема ассоциации (no_assoc), это же и консервативный дефолт для
# нераспознанной причины. Источник перечня причин (NMDeviceStateReason):
# https://lazka.github.io/pgi-docs/NM-1.0/enums.html
#
# ВАЖНО: точный формат строки GENERAL.STATE-REASON у nmcli варьируется между
# версиями (иногда только код, иногда код+текст в скобках) — сюда заложены
# оба варианта написания (kebab-case и с подчёркиванием) как подстроки для
# терпимого сопоставления. Не проверено на живом NetworkManager — сырые
# state/reason всегда логируются на INFO при отказе, чтобы список было легко
# донастроить по факту (см. classify_nm_failure).
_NM_DHCP_FAIL_HINTS = (
    "ip-config-unavailable", "ip_config_unavailable",
    "ip-config-expired", "ip_config_expired",
    "dhcp-start-failed", "dhcp_start_failed",
    "dhcp-error", "dhcp_error",
    "dhcp-failed", "dhcp_failed",
    "dhcp timeout",
)


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


def classify_nm_failure(reason):
    """Классифицирует причину отказа активации NetworkManager в наш статус.

    ``reason`` — строка ``GENERAL.STATE-REASON`` (``nmcli -t -f
    GENERAL.STATE-REASON device show <iface>``) после неудачного
    ``nmcli connection up``.

    Args:
        reason: Сырая строка причины от nmcli (или None/пусто).

    Returns:
        ``"no_dhcp"`` — причина связана с получением IP по DHCP;
        ``"no_assoc"`` — всё остальное, включая нераспознанную причину
        (консервативный дефолт, как и было до перехода на nmcli).
    """
    text = (reason or "").strip().lower()
    if any(hint in text for hint in _NM_DHCP_FAIL_HINTS):
        return "no_dhcp"
    return "no_assoc"


def _build_nmcli_add_cmd(con_name, iface, ap, security):
    """Строит команду ``nmcli connection add`` для временного профиля точки.

    Подключение всегда по имени SSID: у точек оператора нет BSSID (в конфиге
    только SSID + пароль) — цепляемся к сильнейшей точке с этим именем,
    выбор делает сам NetworkManager.

    Args:
        con_name: Уникальное имя временного профиля (удаляется после проверки).
        iface:    Managed-интерфейс, на котором создаётся профиль.
        ap:       Словарь точки (ssid, psk/psk_hash, hidden, ...).
        security: Уже разрешённый тип шифрования (см. _resolve_security):
                  ``open|wep|wpa-psk|wpa2-psk|wpa3-sae``.

    Returns:
        Список токенов команды (без shell — пароль как отдельный argv-элемент,
        экранирование не требуется).
    """
    ssid = ap.get("ssid", "")
    hidden = ap.get("hidden", False)
    secret = ap.get("psk_hash") or ap.get("psk") or ""

    cmd = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "con-name", con_name,
        "ifname", iface,
        "ssid", ssid,
        "connection.autoconnect", "no",
    ]
    if hidden:
        cmd += ["802-11-wireless.hidden", "yes"]

    security = (security or "open").lower()
    if security == "open":
        pass  # без wifi-sec.* — открытая сеть, ключ не нужен
    elif security == "wep":
        cmd += ["wifi-sec.key-mgmt", "none", "wifi-sec.wep-key0", secret]
    elif security == "wpa3-sae":
        cmd += ["wifi-sec.key-mgmt", "sae", "wifi-sec.psk", secret]
    else:
        # wpa-psk / wpa2-psk и запасной случай для нераспознанного типа
        cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", secret]
    return cmd


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class APChecker:
    """
    Проверяет точки доступа из ap_targets.yaml одну за другой.

    Использует отдельный managed-интерфейс (не тот, что у Kismet).
    Пароли никогда не попадают в логи, исключения или возвращаемые данные.
    """

    def __init__(self, iface, targets_path, conn, gps, defaults=None,
                 default_security="wpa2-psk"):
        self.iface = iface
        self.targets_path = targets_path
        self.conn = conn
        self.gps = gps
        # Запасной тип шифрования: если он не задан у точки и не выведен из эфира
        self.default_security = (default_security or "wpa2-psk").lower()
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
    # Проверка одной точки
    # ------------------------------------------------------------------

    def _resolve_security(self, ap, observed_security=None):
        """Определяет тип шифрования точки: конфиг → наблюдение → дефолт.

        Приоритет:
          1. ``ap['security']`` — если оператор явно задал в ap_targets.yaml;
          2. ``observed_security`` — выведенный из эфира (скан/монитор);
          3. ``self.default_security`` — запасной (обычно ``wpa2-psk``).

        Returns:
            Строка типа шифрования в нижнем регистре.
        """
        explicit = (ap.get("security") or "").strip().lower()
        if explicit:
            return explicit
        if observed_security:
            return observed_security.strip().lower()
        return self.default_security

    def check_one(self, ap, observed_security=None):
        """
        Проверка одной точки: подключение по SSID + проверка выхода в интернет.

        BSSID у точек оператора нет — подключаемся по имени сети. Тип шифрования
        разрешается через :meth:`_resolve_security` (конфиг → наблюдение → дефолт).
        Для Enterprise (``wpa2-eap``) одного SSID+пароля недостаточно — сразу
        возвращаем статус ``eap_unsupported``, не тратя попытку и таймауты.

        Args:
            ap:                Словарь точки из ap_targets.yaml.
            observed_security: Тип шифрования, наблюдённый в эфире (или None).

        Returns:
            dict: {ap_id, status, rtt_ms, lat, lon, timestamp}.
            psk/psk_hash НИКОГДА не включаются в результат.
        """
        ap_id = ap.get("id", "unknown")
        security = self._resolve_security(ap, observed_security)

        if security == "wpa2-eap":
            logger.info(
                "[%s] Тип шифрования Enterprise (802.1X) — только SSID+пароль "
                "недостаточно, пропускаем", ap_id,
            )
            return self._make_result(ap_id, "eap_unsupported", None)

        status, rtt_ms = self._try_connect(ap, security)
        return self._make_result(ap_id, status, rtt_ms)

    def _try_connect(self, ap, security):
        """
        Одна попытка подключения через временный connection-профиль NetworkManager.

        Клиентская карта остаётся managed (раздел 7 ТЗ) — ассоциацию и DHCP
        выполняет сам NetworkManager (``nmcli connection up``), а не отдельный
        wpa_supplicant: см. объяснение в докстринге модуля (два wpa_supplicant
        не могут одновременно владеть одним интерфейсом).

        ``security`` — уже разрешённый тип шифрования (см. _resolve_security).

        Возвращает кортеж (status, rtt_ms). Результат-dict формирует
        вызывающий check_one. psk/psk_hash НИКОГДА не логируются.
        """
        ap_id           = ap.get("id", "unknown")
        dhcp_timeout_s  = int(ap.get("dhcp_timeout_s",  self._defaults["dhcp_timeout_s"]))
        check_timeout_s = int(ap.get("check_timeout_s", self._defaults["check_timeout_s"]))
        check_url       = ap.get("check_url", self._defaults["check_url"])

        con_name = "wifi-monitor-check-{}".format(uuid.uuid4().hex[:8])
        status = "error"
        rtt_ms = None
        connection_created = False

        try:
            # ----------------------------------------------------------
            # Шаг 1: создаём временный connection-профиль под эту точку
            # ----------------------------------------------------------
            add_cmd = _build_nmcli_add_cmd(con_name, self.iface, ap, security)
            rc, stdout, stderr = _run(add_cmd, timeout=10)
            if rc != 0:
                logger.warning(
                    "[%s] nmcli connection add не удался (rc=%d): %s",
                    ap_id, rc, stderr.strip(),
                )
                status = "error"
                return status, rtt_ms
            connection_created = True

            # ----------------------------------------------------------
            # Шаг 2: активация — NetworkManager сам делает ассоциацию + DHCP.
            # -w ограничивает ожидание тем же таймаутом, что раньше был на
            # DHCP (ассоциация+DHCP теперь один шаг с точки зрения nmcli).
            # ----------------------------------------------------------
            rc, stdout, stderr = _run(
                ["nmcli", "-w", str(dhcp_timeout_s), "connection", "up",
                 con_name, "ifname", self.iface],
                timeout=dhcp_timeout_s + 10,
            )
            if rc != 0:
                state, reason = self._get_nm_device_state()
                logger.info(
                    "[%s] Активация не удалась: state=%s reason=%s (nmcli: %s)",
                    ap_id, state, reason, stderr.strip(),
                )
                status = classify_nm_failure(reason)
                return status, rtt_ms

            # ----------------------------------------------------------
            # Шаг 3: ждём готовности DNS — реальной попыткой резолва.
            #
            # К моменту "activated" NetworkManager уже настроил IP и DNS
            # из DHCP, но резолвер может быть готов на долю секунды позже —
            # оставляем то же окно 25с защитной сеткой (было нужно для
            # прежнего dhclient-based пути, здесь скорее подстраховка).
            # ----------------------------------------------------------
            import socket
            from urllib.parse import urlparse

            check_host = urlparse(check_url).hostname or ""
            logger.debug("[%s] Подключено, ждём резолва %s...", ap_id, check_host)

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
                logger.debug(
                    "[%s] DNS не резолвит за 25с. resolv.conf:\n%s",
                    ap_id, resolv.strip(),
                )
                status = "no_dns"
                return status, rtt_ms

            # ----------------------------------------------------------
            # Шаг 4: HTTP-проверка
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
                    logger.debug(
                        "[%s] Captive portal или неожиданный код: %d",
                        ap_id, resp.status_code,
                    )
                    status = "captive_portal"
            except requests.exceptions.Timeout:
                rtt_ms = (time.monotonic() - t0) * 1000
                status = "timeout"
            except requests.exceptions.RequestException as exc:
                logger.debug("[%s] HTTP-запрос завершился ошибкой: %s", ap_id, exc)
                status = "no_inet"

        except Exception as exc:
            logger.error("[%s] Неожиданная ошибка при проверке: %s", ap_id, exc)
            status = "error"

        finally:
            # ----------------------------------------------------------
            # Cleanup — выполняется всегда
            # ----------------------------------------------------------
            self._cleanup_nm_connection(con_name, connection_created)

        return status, rtt_ms

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

    def _get_nm_device_state(self):
        """Читает состояние интерфейса из NetworkManager после неудачной активации.

        Возвращает ``(state, reason)`` — сырые строки ``GENERAL.STATE`` /
        ``GENERAL.STATE-REASON`` из ``nmcli device show``, для классификации
        (:func:`classify_nm_failure`) и логирования. При ошибке запроса —
        ``(None, None)``, исключений не бросает.
        """
        rc, stdout, _ = _run(
            ["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.STATE-REASON",
             "device", "show", self.iface],
            timeout=5,
        )
        if rc != 0:
            return None, None
        state, reason = None, None
        for line in stdout.splitlines():
            if line.startswith("GENERAL.STATE:"):
                state = line.split(":", 1)[1].strip()
            elif line.startswith("GENERAL.STATE-REASON:"):
                reason = line.split(":", 1)[1].strip()
        return state, reason

    def _cleanup_nm_connection(self, con_name, created):
        """
        Деактивирует и удаляет временный connection-профиль ``con_name``.
        Все ошибки логируются, не пробрасываются.

        Args:
            con_name: Имя временного профиля (см. _try_connect).
            created:  ``False``, если профиль не был создан (ошибка на самом
                      первом шаге) — тогда удалять нечего.
        """
        if not created:
            return
        _run(["nmcli", "connection", "down", con_name], timeout=10)
        rc, _, stderr = _run(["nmcli", "connection", "delete", con_name], timeout=10)
        if rc != 0:
            logger.warning(
                "Не удалось удалить временный профиль %s: %s", con_name, stderr.strip(),
            )

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
  - <интерфейс> должен быть под управлением NetworkManager (managed, не monitor)
  - nmcli должен быть установлен и NetworkManager запущен
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