"""
src/adapters.py — обнаружение Wi-Fi адаптеров и их возможностей.

Используется из: cli.py.
Не импортирует ничего из других модулей проекта.
Платформа: Kali Linux, требует aircrack-ng/iw/iwconfig/ethtool в PATH.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Внутренние утилиты
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Запускает команду, логирует вызов, возвращает результат.

    Все вызовы — с ``capture_output=True``, ``timeout=5``, ``text=True``.
    При ``TimeoutExpired`` или ``FileNotFoundError`` (нет утилиты в PATH)
    возвращает объект-заглушку с ``returncode=1`` и пустым stdout/stderr,
    чтобы вызывающий код мог единообразно проверять ``returncode``.

    Args:
        cmd: Список токенов команды (без shell=True).

    Returns:
        :class:`subprocess.CompletedProcess` или заглушка при ошибке запуска.
    """
    logger.debug("syscall: %s", " ".join(cmd))
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("syscall failed (%s): %s", " ".join(cmd), exc)
        # Возвращаем заглушку, чтобы не пробрасывать исключение наружу
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")


def _read_file(path: str) -> Optional[str]:
    """Читает текстовый файл и возвращает содержимое без пробелов или None."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _readlink_basename(path: str) -> Optional[str]:
    """Возвращает basename симлинка по указанному пути или None."""
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Сбор полей одного адаптера
# ---------------------------------------------------------------------------

def _is_wifi_iface(iface: str) -> bool:
    """True если интерфейс является Wi-Fi (по /sys или 'iw dev info')."""
    if Path(f"/sys/class/net/{iface}/wireless").exists():
        return True
    result = _run(["iw", "dev", iface, "info"])
    return result.returncode == 0


def _get_mac(iface: str) -> str:
    """Читает MAC-адрес из /sys/class/net/<iface>/address."""
    return _read_file(f"/sys/class/net/{iface}/address") or "unknown"


def _get_driver(iface: str) -> str:
    """Определяет имя драйвера через симлинк device/driver/module."""
    name = _readlink_basename(f"/sys/class/net/{iface}/device/driver/module")
    return name or "unknown"


def _get_chipset(iface: str) -> str:
    """Определяет чипсет через ethtool, затем через lsusb (best-effort).

    Порядок попыток:
    1. ``ethtool -i <iface>`` — поле ``driver:``.
    2. USB VID:PID → ``lsusb -d VID:PID`` — описание устройства.
    """
    # --- Попытка 1: ethtool -i ---
    result = _run(["ethtool", "-i", iface])
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("driver:"):
                chipset = line.split(":", 1)[1].strip()
                if chipset:
                    logger.debug("chipset via ethtool: %s → %s", iface, chipset)
                    return chipset

    # --- Попытка 2: lsusb через VID:PID из sysfs ---
    try:
        device_real   = os.path.realpath(f"/sys/class/net/{iface}/device")
        device_parent = os.path.dirname(device_real)
        # Для USB-интерфейса (1-1.3:1.0) VID/PID находятся на уровень выше
        for candidate_dir in (device_real, device_parent):
            vid = _read_file(os.path.join(candidate_dir, "idVendor"))
            pid = _read_file(os.path.join(candidate_dir, "idProduct"))
            if vid and pid:
                lsusb = _run(["lsusb", "-d", f"{vid}:{pid}"])
                if lsusb.returncode == 0:
                    line = lsusb.stdout.strip().splitlines()[0] if lsusb.stdout.strip() else ""
                    # "Bus 002 Device 003: ID 0bda:8812 Realtek ... RTL8812AU ..."
                    # Берём всё після "ID xx:xx "
                    if " ID " in line:
                        after_id = line.split(" ID ", 1)[1]           # "0bda:8812 Realtek ..."
                        chipset  = after_id.split(" ", 1)[1].strip()  # "Realtek ..."
                        if chipset:
                            logger.debug("chipset via lsusb: %s → %s", iface, chipset)
                            return chipset
    except Exception as exc:
        logger.debug("_get_chipset lsusb fallback failed for %s: %s", iface, exc)

    return "unknown"


def _get_usb_path(iface: str) -> Optional[str]:
    """Возвращает короткий USB-путь (напр. 'usb1/1-1.3') или None.

    Разрешает симлинк /sys/class/net/<iface>/device и, если в пути есть
    'usb', извлекает компоненты «usb<N>/<порт>».
    """
    try:
        device_sym = f"/sys/class/net/{iface}/device"
        if not os.path.exists(device_sym):
            return None
        real = os.path.realpath(device_sym)
        if "usb" not in real:
            return None
        parts = real.split("/")
        for i, part in enumerate(parts):
            if part.startswith("usb") and part[3:].isdigit():
                # Берём usb<N> и следующий компонент (порт/устройство)
                tail = parts[i + 1] if i + 1 < len(parts) else ""
                return f"{part}/{tail}" if tail else part
        # 'usb' присутствует, но паттерн usb<N> не найден — возвращаем «usb»
        return "usb"
    except Exception as exc:
        logger.debug("_get_usb_path failed for %s: %s", iface, exc)
        return None


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def check_monitor_support(iface: str) -> bool:
    """Проверяет поддержку monitor mode для указанного интерфейса.

    Алгоритм (первый успешный результат → возврат True):

    1. ``iw phy <phy> info`` — ищем ``monitor`` в секции
       «Supported interface modes».  PHY-имя берётся из симлинка
       ``/sys/class/net/<iface>/phy80211``.
    2. ``iwconfig <iface>`` — наличие строки ``Mode:Monitor``
       (адаптер уже переведён в monitor mode).

    При любом исключении возвращает ``False``, ничего не пробрасывает.

    Args:
        iface: Имя сетевого интерфейса (например ``wlan0``).

    Returns:
        ``True`` если monitor mode поддерживается или уже активен.
    """
    # --- Способ 1: iw phy info ---
    try:
        phy_link = f"/sys/class/net/{iface}/phy80211"
        if Path(phy_link).exists():
            phy = _readlink_basename(phy_link)
            if phy:
                result = _run(["iw", "phy", phy, "info"])
                if result.returncode == 0:
                    in_modes = False
                    for line in result.stdout.splitlines():
                        if "Supported interface modes" in line:
                            in_modes = True
                            section_indent = len(line) - len(line.lstrip())
                            continue
                        if in_modes:
                            stripped    = line.strip()
                            line_indent = len(line) - len(line.lstrip())
                            if not stripped:
                                continue
                            if line_indent <= section_indent:
                                # Отступ вернулся к уровню заголовка секции
                                # (напр. \tBand 1:) — вышли из секции режимов
                                in_modes = False
                            elif stripped.startswith("*"):
                                if "monitor" in stripped.lower():
                                    logger.debug(
                                        "monitor supported (iw phy): %s → %s", iface, phy
                                    )
                                    return True
    except Exception as exc:
        logger.debug("check_monitor_support iw phy failed for %s: %s", iface, exc)

    # --- Способ 2: iwconfig ---
    try:
        result = _run(["iwconfig", iface])
        if result.returncode == 0 and "Mode:Monitor" in result.stdout:
            logger.debug("monitor active (iwconfig): %s", iface)
            return True
    except Exception as exc:
        logger.debug("check_monitor_support iwconfig failed for %s: %s", iface, exc)

    return False


def list_wifi_adapters() -> list[dict]:
    """Возвращает список всех Wi-Fi адаптеров в системе.

    Для каждого интерфейса собирает словарь:

    - ``iface``            (str)       — имя интерфейса (``wlan0``, …);
    - ``mac``              (str)       — MAC-адрес;
    - ``driver``           (str)       — имя модуля ядра или ``'unknown'``;
    - ``chipset``          (str)       — название чипсета или ``'unknown'``;
    - ``supports_monitor`` (bool)      — поддержка monitor mode;
    - ``usb_path``         (str|None)  — короткий USB-путь или ``None``.

    Никогда не поднимает исключений: при ошибке чтения любого поля
    подставляет ``'unknown'`` / ``None`` и продолжает.

    Returns:
        Список словарей, по одному на каждый найденный Wi-Fi интерфейс.
        Пустой список если Wi-Fi адаптеры не найдены.
    """
    adapters: list[dict] = []

    try:
        iface_names = os.listdir("/sys/class/net")
    except OSError as exc:
        logger.error("Не удалось прочитать /sys/class/net: %s", exc)
        return adapters

    for iface in sorted(iface_names):
        if not _is_wifi_iface(iface):
            logger.debug("пропуск не-Wi-Fi интерфейса: %s", iface)
            continue

        logger.debug("обнаружен Wi-Fi интерфейс: %s", iface)
        try:
            adapter: dict = {
                "iface":            iface,
                "mac":              _get_mac(iface),
                "driver":           _get_driver(iface),
                "chipset":          _get_chipset(iface),
                "supports_monitor": check_monitor_support(iface),
                "usb_path":         _get_usb_path(iface),
            }
        except Exception as exc:
            # Последний рубеж: если упало что-то неожиданное — не теряем iface
            logger.warning("Ошибка сбора данных для %s: %s", iface, exc)
            adapter = {
                "iface":            iface,
                "mac":              "unknown",
                "driver":           "unknown",
                "chipset":          "unknown",
                "supports_monitor": False,
                "usb_path":         None,
            }

        adapters.append(adapter)
        logger.debug("адаптер: %s", adapter)

    return adapters


def find_by_mac_or_iface(identifier: str, adapters: list[dict]) -> Optional[dict]:
    """Ищет адаптер в списке по имени интерфейса или MAC-адресу.

    Сравнение регистронезависимое (case-insensitive).  Используется
    из ``cli.py`` для разбора аргументов командной строки.

    Args:
        identifier: Имя интерфейса (``wlan0``) или MAC (``AA:BB:CC:DD:EE:FF``).
        adapters:   Список словарей, возвращённых :func:`list_wifi_adapters`.

    Returns:
        Первый подходящий словарь или ``None`` если ничего не найдено.
    """
    needle = identifier.strip().lower()
    for adapter in adapters:
        if adapter.get("iface", "").lower() == needle:
            return adapter
        if adapter.get("mac", "").lower() == needle:
            return adapter
    return None


def format_adapters_table(adapters: list[dict]) -> str:
    """Форматирует список адаптеров в выровненную текстовую таблицу.

    Пример вывода::

        №   Интерфейс  MAC                Драйвер/Чипсет     Monitor  USB
        ─── ─────────  ─────────────────  ─────────────────  ───────  ────────
        1   wlan0      AA:BB:CC:DD:EE:FF  rtl8812au          ✓        usb1/1.3
        2   wlan1      11:22:33:44:55:66  ath9k_htc          ✗        usb1/1.4

    Использует только ``str.ljust`` / ``str.rjust`` из стандартной библиотеки.

    Args:
        adapters: Список словарей от :func:`list_wifi_adapters`.

    Returns:
        Многострочная строка таблицы (без завершающего ``\\n``).
    """
    if not adapters:
        return "(Wi-Fi адаптеры не найдены)"

    # --- Подготовка строк данных ---
    rows: list[tuple[str, str, str, str, str, str]] = []
    for i, a in enumerate(adapters, start=1):
        driver_chip = a.get("chipset") or a.get("driver") or "unknown"
        # Если chipset и driver оба известны и различаются — показываем chipset
        if (
            a.get("chipset", "unknown") not in ("unknown", "")
            and a.get("driver", "unknown") not in ("unknown", "")
            and a["chipset"] != a["driver"]
        ):
            driver_chip = a["chipset"]
        else:
            driver_chip = a.get("driver", "unknown")

        rows.append((
            str(i),
            a.get("iface", "?"),
            a.get("mac", "unknown"),
            driver_chip,
            "✓" if a.get("supports_monitor") else "✗",
            a.get("usb_path") or "—",
        ))

    headers = ("№", "Интерфейс", "MAC", "Драйвер/Чипсет", "Monitor", "USB")

    # --- Вычисление ширин колонок ---
    col_widths = [len(h) for h in headers]
    for row in rows:
        for j, cell in enumerate(row):
            col_widths[j] = max(col_widths[j], len(cell))

    # Добавляем отступ
    col_widths = [w + 1 for w in col_widths]

    def fmt_row(cells: tuple) -> str:
        return "  ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells)).rstrip()

    sep = "  ".join("─" * col_widths[i] for i in range(len(headers))).rstrip()

    lines = [fmt_row(headers), sep]
    for row in rows:
        lines.append(fmt_row(row))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Демонстрационный запуск
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("Сканирование Wi-Fi адаптеров...\n")
    found = list_wifi_adapters()
    print(format_adapters_table(found))

    if found:
        print(f"\nНайдено адаптеров: {len(found)}")
        monitor_capable = [a for a in found if a["supports_monitor"]]
        print(f"С поддержкой monitor mode: {len(monitor_capable)}")