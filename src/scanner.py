# -*- coding: utf-8 -*-
"""
src/scanner.py

Активный скан эфира клиентской (managed) картой и нормализация типа шифрования.

Используется из src/cli.py в режиме 2: перед проверкой точек оператора карта
сканирует эфир (`iw dev <iface> scan`), и проверяются ТОЛЬКО те цели, которые
реально видны сейчас — так мы не тратим таймауты на отсутствующие точки.

Тип шифрования (`security`) точки берётся из наблюдения (beacon), а не из конфига:
у оператора в файле есть только SSID и пароль. `parse_security` / `normalize_security`
приводят наблюдённый тип к единому набору:

    open | wpa-psk | wpa2-psk | wpa3-sae | wpa2-eap | wep

Замечание по ТЗ §2: скан ищет СВОИ точки (подключаемся тоже только к своим);
к чужим сетям не подключаемся и пароли не проверяем.
"""

import logging
import re
import subprocess

logger = logging.getLogger(__name__)

# Канонические типы шифрования, которые понимает ap_checker._write_wpa_conf
SEC_OPEN = "open"
SEC_WPA = "wpa-psk"
SEC_WPA2 = "wpa2-psk"
SEC_WPA3 = "wpa3-sae"
SEC_EAP = "wpa2-eap"
SEC_WEP = "wep"

_BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})")
_SIGNAL_RE = re.compile(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm")
_FREQ_RE = re.compile(r"freq:\s*(\d+)")
_SSID_RE = re.compile(r"SSID:\s?(.*)")


# ---------------------------------------------------------------------------
# Вспомогательный запуск команд
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    """Запускает команду, возвращает (rc, stdout, stderr). Не бросает исключений."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "not found: {}".format(cmd[0])
    except Exception as exc:  # noqa: BLE001 - best-effort, ошибку отдаём наверх строкой
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# Нормализация типа шифрования
# ---------------------------------------------------------------------------

def normalize_security(text):
    """Приводит произвольную строку шифрования к каноническому виду.

    Источник — поле ``encryption`` из таблицы ``networks`` (его пишет Kismet-путь
    из ``kismet.device.base.crypt``), напр. ``"WPA2-PSK"``, ``"WPA3-SAE"``,
    ``"WPA2-PSK-SAE"`` (transition), ``"WPA2-EAP"``, ``"Open"``, ``"WEP"``.

    WPA3-transition (одновременно PSK и SAE) трактуется как ``wpa2-psk`` для
    широкой совместимости: wpa_supplicant с WPA-PSK ассоциируется к таким точкам.

    Args:
        text: Строка описания шифрования (регистр не важен) или ``None``.

    Returns:
        Один из ``open|wpa-psk|wpa2-psk|wpa3-sae|wpa2-eap|wep`` либо ``None``,
        если распознать не удалось (тогда вызывающий код берёт ``default_security``).
    """
    if not text:
        return None
    s = str(text).upper()

    has_psk = "PSK" in s
    has_sae = "SAE" in s
    has_eap = "EAP" in s or "802.1X" in s or "802.11X" in s or "ENTERPRISE" in s

    # Enterprise: только SSID+пароль недостаточно — помечаем как EAP (не поддержано)
    if has_eap and not has_psk:
        return SEC_EAP
    # Чистый WPA3-SAE (без PSK) — нужен key_mgmt=SAE
    if has_sae and not has_psk:
        return SEC_WPA3
    # PSK (в т.ч. transition PSK+SAE) — самый распространённый случай
    if has_psk:
        # Старый WPA1 без RSN/WPA2/WPA3 → wpa-psk (proto WPA)
        if "WPA2" in s or "WPA3" in s or "RSN" in s:
            return SEC_WPA2
        if s.strip() in ("WPA", "WPA-PSK", "WPA1", "WPA1-PSK"):
            return SEC_WPA
        return SEC_WPA2
    if "WPA3" in s:
        return SEC_WPA3
    if "WPA" in s or "RSN" in s:
        return SEC_WPA2
    if "WEP" in s:
        return SEC_WEP
    if "OPEN" in s or "NONE" in s:
        return SEC_OPEN
    return None


def parse_security(cell_lines):
    """Определяет тип шифрования по capability-блоку одной BSS из вывода ``iw scan``.

    Смотрит на наличие блоков ``RSN:`` / ``WPA:``, перечисленные
    ``Authentication suites:`` (PSK / SAE / IEEE 802.1X) и флаг ``Privacy`` в
    строке ``capability:``.

    Args:
        cell_lines: Список строк одной ячейки BSS (между заголовками ``BSS ...``).

    Returns:
        Канонический тип: ``open|wpa-psk|wpa2-psk|wpa3-sae|wpa2-eap|wep``.
    """
    has_rsn = False
    has_wpa = False
    auth = set()
    privacy = False

    for line in cell_lines:
        stripped = line.strip()
        low = stripped.lower()
        if stripped.startswith("RSN:"):
            has_rsn = True
        elif stripped.startswith("WPA:"):
            has_wpa = True
        elif "authentication suites:" in low:
            suites = low.split("authentication suites:", 1)[1]
            if "802.1x" in suites or "eap" in suites:
                auth.add("EAP")
            if "sae" in suites:
                auth.add("SAE")
            if "psk" in suites:
                auth.add("PSK")
        elif low.startswith("capability:") and "privacy" in low:
            privacy = True

    if has_rsn or has_wpa:
        if "EAP" in auth and "PSK" not in auth:
            return SEC_EAP
        if "SAE" in auth and "PSK" not in auth:
            return SEC_WPA3
        if "PSK" in auth:
            return SEC_WPA2 if has_rsn else SEC_WPA
        # Блок защиты есть, но конкретный auth-suite не разобрали — считаем PSK
        return SEC_WPA2 if has_rsn else SEC_WPA

    # Ни RSN, ни WPA
    return SEC_WEP if privacy else SEC_OPEN


# ---------------------------------------------------------------------------
# Разбор вывода iw scan
# ---------------------------------------------------------------------------

def parse_scan(text):
    """Разбирает вывод ``iw dev <iface> scan`` в список видимых сетей.

    Args:
        text: Полный stdout команды ``iw dev <iface> scan``.

    Returns:
        Список словарей ``{bssid, ssid, signal, freq, security}``:
          - ``bssid``    (str)       — MAC точки;
          - ``ssid``     (str)       — имя сети (``""`` для скрытых);
          - ``signal``   (int|None)  — RSSI в dBm (округлён);
          - ``freq``     (int|None)  — частота в МГц;
          - ``security`` (str)       — канонический тип шифрования (см. parse_security).
    """
    networks = []
    if not text:
        return networks

    cur = None
    cur_lines = []

    def _flush():
        if cur is None:
            return
        cur["security"] = parse_security(cur_lines)
        networks.append(cur)

    for line in text.splitlines():
        m = _BSS_RE.match(line.strip())
        if m:
            _flush()
            cur = {"bssid": m.group(1).lower(), "ssid": "", "signal": None, "freq": None}
            cur_lines = []
            continue
        if cur is None:
            continue
        cur_lines.append(line)

        sig = _SIGNAL_RE.search(line)
        if sig:
            try:
                cur["signal"] = int(round(float(sig.group(1))))
            except ValueError:
                pass
            continue
        fr = _FREQ_RE.search(line)
        if fr and cur["freq"] is None:
            try:
                cur["freq"] = int(fr.group(1))
            except ValueError:
                pass
            continue
        ss = _SSID_RE.search(line.strip())
        if ss and line.strip().startswith("SSID:"):
            cur["ssid"] = ss.group(1)

    _flush()
    return networks


def strongest_by_ssid(networks):
    """Схлопывает список сетей в ``{ssid: {bssid, signal, security}}`` по сильнейшему RSSI.

    Сети без SSID (скрытые) пропускаются — по имени их не сматчить с целью.

    Args:
        networks: Список словарей, как из :func:`parse_scan`.

    Returns:
        Словарь SSID → информация о сильнейшем наблюдении этой сети.
    """
    best = {}
    for net in networks:
        ssid = net.get("ssid")
        if not ssid:
            continue
        signal = net.get("signal")
        prev = best.get(ssid)
        if prev is None or (signal is not None and (prev["signal"] is None or signal > prev["signal"])):
            best[ssid] = {
                "bssid":    net.get("bssid"),
                "signal":   signal,
                "security": net.get("security"),
            }
    return best


# ---------------------------------------------------------------------------
# Активный скан
# ---------------------------------------------------------------------------

def scan_visible(iface, timeout=20):
    """Сканирует эфир картой ``iface`` и возвращает видимые сети.

    Поднимает интерфейс (``ip link set <iface> up``), затем выполняет
    ``iw dev <iface> scan``. Требует root. Ошибку (в т.ч. «Device or resource
    busy», если карта занята ассоциацией) НЕ пробрасывает — возвращает пустой
    список, чтобы цикл проверки продолжился на следующей итерации.

    Args:
        iface:   Имя managed-интерфейса (например ``wlan1``).
        timeout: Таймаут на сам скан, секунды.

    Returns:
        Список словарей из :func:`parse_scan` (пустой при ошибке).
    """
    _run(["ip", "link", "set", iface, "up"], timeout=5)

    rc, stdout, stderr = _run(["iw", "dev", iface, "scan"], timeout=timeout)
    if rc != 0:
        logger.info("Скан %s не удался (rc=%d): %s", iface, rc, (stderr or "").strip())
        return []

    networks = parse_scan(stdout)
    logger.debug("Скан %s: видно %d сетей", iface, len(networks))
    return networks


# ---------------------------------------------------------------------------
# Ручной запуск для отладки
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    if len(sys.argv) < 2:
        print("Использование: sudo python3 -m src.scanner <iface>")
        sys.exit(1)

    for n in scan_visible(sys.argv[1]):
        print("{:<20} {:<28} {:>4} dBm  {:<10} {}".format(
            n["bssid"], (n["ssid"] or "<hidden>")[:27],
            n["signal"] if n["signal"] is not None else "?",
            n["security"], n["freq"] or "",
        ))
