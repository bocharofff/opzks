"""
tests/test_adapters.py — тесты для src/adapters.py
"""

import subprocess
from unittest.mock import patch

import pytest

from src.adapters import (
    _run,
    _get_chipset,
    _get_usb_path,
    check_monitor_support,
    find_by_mac_or_iface,
    format_adapters_table,
    list_wifi_adapters,
)


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

# Реалистичный вывод iw phy info — monitor mode присутствует
IW_PHY_WITH_MONITOR = """\
Wiphy phy0
\tmax # scan SSIDs: 4
\tSupported interface modes:
\t\t * IBSS
\t\t * managed
\t\t * AP
\t\t * AP/VLAN
\t\t * monitor
\t\t * mesh point
\tBand 1:
\t\tCapabilities: 0x116e
"""

# Вывод без monitor (например, встроенный чипсет ноутбука)
IW_PHY_NO_MONITOR = """\
Wiphy phy1
\tSupported interface modes:
\t\t * managed
\t\t * AP
\t\t * P2P-client
\t\t * P2P-GO
\tBand 1:
\t\tCapabilities: 0x1062
"""

# ethtool -i wlan0
ETHTOOL_OUTPUT = """\
driver: rtl8812au
version: 5.2.20
firmware-version:
expansion-rom-version:
bus-info: 2-1.3:1.0
supports-statistics: no
supports-test: no
supports-eeprom-access: no
supports-register-dump: no
supports-priv-flags: no
"""

# ethtool без поля driver (пустое значение)
ETHTOOL_NO_DRIVER = """\
driver:
version: 0.1
bus-info: 2-1.4:1.0
"""

# lsusb -d 0bda:8812
LSUSB_OUTPUT = (
    "Bus 002 Device 003: ID 0bda:8812 "
    "Realtek Semiconductor Corp. RTL8812AU 802.11ac WLAN"
)

# Путь в sysfs для USB-адаптера
USB_SYS_PATH = (
    "/sys/devices/pci0000:00/0000:00:1a.0/usb1/1-1/1-1.3/1-1.3:1.0"
)
# Путь для PCI-адаптера (нет usb)
PCI_SYS_PATH = "/sys/devices/pci0000:00/0000:00:1c.0/0000:02:00.0/net/wlan0"


def _completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    """Быстрый конструктор заглушки CompletedProcess."""
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# _run — обёртка над subprocess.run
# ---------------------------------------------------------------------------

def test_run_returns_output_on_success():
    """Успешный вызов: returncode=0, stdout доступен."""
    with patch("subprocess.run", return_value=_completed("ok\n", 0)) as mock:
        result = _run(["echo", "ok"])
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_run_passes_timeout_5():
    """_run обязан передавать timeout=5 в subprocess.run."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _completed("", 0)

    with patch("subprocess.run", side_effect=fake_run):
        _run(["iw", "dev"])

    assert captured.get("timeout") == 5


def test_run_timeout_returns_stub_not_raises():
    """TimeoutExpired не пробрасывается наружу — возвращается заглушка rc=1."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("iw", 5)):
        result = _run(["iw", "dev", "wlan0", "info"])
    assert result.returncode == 1
    assert result.stdout == ""


def test_run_file_not_found_returns_stub_not_raises():
    """FileNotFoundError (утилита не установлена) → заглушка, не исключение."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = _run(["nonexistent_tool", "--version"])
    assert result.returncode == 1
    assert result.stdout == ""


def test_run_uses_capture_output():
    """_run должен использовать capture_output=True."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _completed()

    with patch("subprocess.run", side_effect=fake_run):
        _run(["iw"])

    assert captured.get("capture_output") is True


# ---------------------------------------------------------------------------
# check_monitor_support — iw phy info
# ---------------------------------------------------------------------------

def test_check_monitor_found_via_iw_phy():
    """monitor есть в 'Supported interface modes' → True."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("os.readlink", return_value="../../../ieee80211/phy0"), \
         patch("src.adapters._run", return_value=_completed(IW_PHY_WITH_MONITOR)):
        assert check_monitor_support("wlan0") is True


def test_check_monitor_absent_in_iw_phy():
    """monitor отсутствует в секции режимов → iw даёт False, iwconfig тоже."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("os.readlink", return_value="../../../ieee80211/phy1"), \
         patch("src.adapters._run", return_value=_completed(IW_PHY_NO_MONITOR)):
        assert check_monitor_support("wlan1") is False


def test_check_monitor_stops_at_next_section():
    """'monitor' вне секции (например, в имени частоты) не засчитывается."""
    iw_output = """\
Wiphy phy0
\tSupported interface modes:
\t\t * managed
\t\t * AP
\tBand 1:
\t\t* monitor-like-freq: 5180 MHz
"""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("os.readlink", return_value="../../../ieee80211/phy0"), \
         patch("src.adapters._run", return_value=_completed(iw_output)):
        assert check_monitor_support("wlan0") is False


# ---------------------------------------------------------------------------
# check_monitor_support — iwconfig fallback
# ---------------------------------------------------------------------------

def test_check_monitor_fallback_iwconfig_mode_monitor():
    """phy80211 недоступен, iwconfig показывает Mode:Monitor → True."""
    with patch("pathlib.Path.exists", return_value=False), \
         patch("src.adapters._run", return_value=_completed(
             "wlan0     IEEE 802.11  Mode:Monitor  Frequency:2.437 GHz"
         )):
        assert check_monitor_support("wlan0") is True


def test_check_monitor_fallback_iwconfig_managed_mode():
    """iwconfig показывает Mode:Managed (не Monitor) → False."""
    with patch("pathlib.Path.exists", return_value=False), \
         patch("src.adapters._run", return_value=_completed(
             "wlan0     IEEE 802.11  Mode:Managed  Frequency:2.437 GHz"
         )):
        assert check_monitor_support("wlan0") is False


def test_check_monitor_iw_rc_nonzero_falls_through_to_iwconfig():
    """iw phy вернул rc!=0 → fallback на iwconfig, который даёт Mode:Monitor."""
    call_log = []

    def side(cmd):
        call_log.append(cmd)
        if "phy" in cmd:
            return _completed("", 1)          # iw phy провалился
        return _completed("Mode:Monitor", 0)  # iwconfig успешен

    with patch("pathlib.Path.exists", return_value=True), \
         patch("os.readlink", return_value="../../../ieee80211/phy0"), \
         patch("src.adapters._run", side_effect=side):
        result = check_monitor_support("wlan0")

    assert result is True
    assert any("phy" in c for c in call_log)    # iw phy был вызван
    assert any("iwconfig" in c for c in call_log)  # iwconfig тоже


def test_check_monitor_exception_returns_false():
    """Любое неожиданное исключение → False, не пробрасывается наружу."""
    with patch("pathlib.Path.exists", side_effect=RuntimeError("kernel panic")):
        assert check_monitor_support("wlan0") is False


# ---------------------------------------------------------------------------
# _get_chipset — ethtool → lsusb цепочка
# ---------------------------------------------------------------------------

def test_get_chipset_via_ethtool():
    """ethtool возвращает driver: rtl8812au → результат 'rtl8812au'."""
    with patch("src.adapters._run", return_value=_completed(ETHTOOL_OUTPUT)):
        assert _get_chipset("wlan0") == "rtl8812au"


def test_get_chipset_ethtool_empty_driver_falls_to_lsusb():
    """ethtool возвращает пустой driver: → переходим к lsusb."""
    def side(cmd):
        if "ethtool" in cmd:
            return _completed(ETHTOOL_NO_DRIVER, 0)
        if "lsusb" in cmd:
            return _completed(LSUSB_OUTPUT, 0)
        return _completed("", 1)

    with patch("src.adapters._run",       side_effect=side), \
         patch("os.path.realpath",        return_value="/sys/bus/usb/2-1.3:1.0"), \
         patch("os.path.dirname",         return_value="/sys/bus/usb/2-1.3"), \
         patch("src.adapters._read_file", side_effect=lambda p: (
             "0bda" if "idVendor" in p else
             "8812" if "idProduct" in p else None
         )):
        result = _get_chipset("wlan0")

    assert "Realtek" in result


def test_get_chipset_lsusb_parses_description_after_id():
    """lsusb: берём текст после 'ID xx:xx', игнорируем Bus/Device prefix."""
    def side(cmd):
        if "ethtool" in cmd:
            return _completed("", 1)
        return _completed(LSUSB_OUTPUT, 0)

    with patch("src.adapters._run",       side_effect=side), \
         patch("os.path.realpath",        return_value="/sys/bus/usb/2-1.3:1.0"), \
         patch("os.path.dirname",         return_value="/sys/bus/usb/2-1.3"), \
         patch("src.adapters._read_file", side_effect=lambda p: (
             "0bda" if "idVendor" in p else
             "8812" if "idProduct" in p else None
         )):
        result = _get_chipset("wlan0")

    # Не должен начинаться с "Bus" или "ID 0bda:8812"
    assert not result.startswith("Bus")
    assert not result.startswith("0bda")
    assert "RTL8812AU" in result or "Realtek" in result


def test_get_chipset_both_fail_returns_unknown():
    """ethtool и lsusb оба недоступны → 'unknown'."""
    with patch("src.adapters._run",       return_value=_completed("", 1)), \
         patch("src.adapters._read_file", return_value=None):
        assert _get_chipset("wlan0") == "unknown"


def test_get_chipset_no_vid_pid_skips_lsusb():
    """VID/PID не найдены в sysfs → lsusb не вызывается, итог 'unknown'."""
    lsusb_calls = []

    def side(cmd):
        if "lsusb" in cmd:
            lsusb_calls.append(cmd)
        return _completed("", 1)

    with patch("src.adapters._run",       side_effect=side), \
         patch("os.path.realpath",        return_value="/sys/bus/usb/2-1.3:1.0"), \
         patch("os.path.dirname",         return_value="/sys/bus/usb/2-1.3"), \
         patch("src.adapters._read_file", return_value=None):
        result = _get_chipset("wlan0")

    assert result == "unknown"
    assert lsusb_calls == []


# ---------------------------------------------------------------------------
# _get_usb_path — извлечение usb<N>/<порт> из sysfs realpath
# ---------------------------------------------------------------------------

def test_get_usb_path_standard_usb_device():
    """Типичный USB-путь → возвращает 'usb1/<порт>'."""
    with patch("os.path.exists",   return_value=True), \
         patch("os.path.realpath", return_value=USB_SYS_PATH):
        result = _get_usb_path("wlan0")
    assert result is not None
    assert result.startswith("usb1")


def test_get_usb_path_contains_port_component():
    """Результат включает второй компонент (порт), не только 'usb1'."""
    with patch("os.path.exists",   return_value=True), \
         patch("os.path.realpath", return_value=USB_SYS_PATH):
        result = _get_usb_path("wlan0")
    assert "/" in result           # formат usb<N>/<port>


def test_get_usb_path_pci_device_returns_none():
    """PCI-адаптер (нет 'usb' в пути) → None."""
    with patch("os.path.exists",   return_value=True), \
         patch("os.path.realpath", return_value=PCI_SYS_PATH):
        assert _get_usb_path("wlan0") is None


def test_get_usb_path_device_absent_returns_none():
    """Если /sys/class/net/<iface>/device не существует → None."""
    with patch("os.path.exists", return_value=False):
        assert _get_usb_path("wlan0") is None


def test_get_usb_path_exception_returns_none():
    """Любое исключение при обращении к sysfs → None, не пробрасывается."""
    with patch("os.path.exists", side_effect=OSError("permission denied")):
        assert _get_usb_path("wlan0") is None


# ---------------------------------------------------------------------------
# find_by_mac_or_iface
# ---------------------------------------------------------------------------

def test_find_by_iface_exact(sample_adapters):
    """Поиск по точному имени интерфейса возвращает нужный адаптер."""
    result = find_by_mac_or_iface("wlan0", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan0"


def test_find_by_iface_case_insensitive(sample_adapters):
    """Имя интерфейса в верхнем регистре совпадает с 'wlan1'."""
    result = find_by_mac_or_iface("WLAN1", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan1"


def test_find_by_mac_exact(sample_adapters):
    """Поиск по MAC возвращает правильный адаптер."""
    result = find_by_mac_or_iface("AA:BB:CC:DD:EE:FF", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan0"


def test_find_by_mac_case_insensitive(sample_adapters):
    """MAC в нижнем регистре находит адаптер с MAC в верхнем."""
    result = find_by_mac_or_iface("aa:bb:cc:dd:ee:ff", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan0"


def test_find_by_mac_second_adapter(sample_adapters):
    """Поиск второго адаптера по MAC."""
    result = find_by_mac_or_iface("11:22:33:44:55:66", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan1"


def test_find_nonexistent_returns_none(sample_adapters):
    """Несуществующий идентификатор → None."""
    assert find_by_mac_or_iface("wlan99", sample_adapters) is None


def test_find_in_empty_list_returns_none():
    """Поиск в пустом списке → None, без исключений."""
    assert find_by_mac_or_iface("wlan0", []) is None


def test_find_strips_whitespace(sample_adapters):
    """Идентификатор с пробелами по краям всё равно находит адаптер."""
    result = find_by_mac_or_iface("  wlan0  ", sample_adapters)
    assert result is not None
    assert result["iface"] == "wlan0"


# ---------------------------------------------------------------------------
# format_adapters_table
# ---------------------------------------------------------------------------

def test_table_contains_all_headers(sample_adapters):
    """Заголовки всех шести колонок присутствуют в таблице."""
    table = format_adapters_table(sample_adapters)
    for header in ("№", "Интерфейс", "MAC", "Драйвер/Чипсет", "Monitor", "USB"):
        assert header in table


def test_table_contains_iface_and_mac(sample_adapters):
    """Имена интерфейсов и MAC-адреса отображаются в строках данных."""
    table = format_adapters_table(sample_adapters)
    assert "wlan0"             in table
    assert "wlan1"             in table
    assert "AA:BB:CC:DD:EE:FF" in table
    assert "11:22:33:44:55:66" in table


def test_table_monitor_true_shows_checkmark(sample_adapters):
    """supports_monitor=True отображается как ✓."""
    table = format_adapters_table(sample_adapters)
    assert "✓" in table


def test_table_monitor_false_shows_cross(sample_adapters):
    """supports_monitor=False отображается как ✗."""
    table = format_adapters_table(sample_adapters)
    assert "✗" in table


def test_table_usb_path_shown(sample_adapters):
    """USB-путь адаптера отображается в колонке USB."""
    table = format_adapters_table(sample_adapters)
    assert "usb1/1-1.3" in table
    assert "usb1/1.4"   in table


def test_table_no_usb_shows_dash(single_adapter):
    """usb_path=None отображается как '—', а не пустой строкой."""
    table = format_adapters_table([single_adapter])
    assert "—" in table


def test_table_has_separator_line(sample_adapters):
    """Между заголовком и данными есть строка-разделитель с '─'."""
    table = format_adapters_table(sample_adapters)
    assert "─" in table


def test_table_row_count(sample_adapters):
    """Две строки данных + заголовок + разделитель = 4 строки всего."""
    lines = format_adapters_table(sample_adapters).strip().splitlines()
    assert len(lines) == 4


def test_table_row_numbering(sample_adapters):
    """Строки нумеруются с 1."""
    table = format_adapters_table(sample_adapters)
    lines = table.strip().splitlines()
    assert lines[2].strip().startswith("1")
    assert lines[3].strip().startswith("2")


def test_table_empty_list_returns_message():
    """Пустой список адаптеров → понятное сообщение, не исключение."""
    result = format_adapters_table([])
    assert "не найден" in result


def test_table_single_adapter_no_crash(single_adapter):
    """Один адаптер в списке форматируется без ошибок."""
    table = format_adapters_table([single_adapter])
    assert "wlan0" in table


# ---------------------------------------------------------------------------
# list_wifi_adapters — интеграционный мок
# ---------------------------------------------------------------------------

def test_list_filters_non_wifi_interfaces():
    """eth0 и lo пропускаются, возвращается только wlan0."""
    with patch("os.listdir",              return_value=["eth0", "lo", "wlan0"]), \
         patch("src.adapters._is_wifi_iface",        side_effect=lambda i: i == "wlan0"), \
         patch("src.adapters._get_mac",              return_value="aa:bb:cc:dd:ee:ff"), \
         patch("src.adapters._get_driver",           return_value="rtl8812au"), \
         patch("src.adapters._get_chipset",          return_value="rtl8812au"), \
         patch("src.adapters.check_monitor_support", return_value=True), \
         patch("src.adapters._get_usb_path",         return_value="usb1/1.3"):
        result = list_wifi_adapters()

    assert len(result) == 1
    assert result[0]["iface"] == "wlan0"


def test_list_result_has_required_keys():
    """Каждый элемент содержит все обязательные ключи."""
    required = {"iface", "mac", "driver", "chipset", "supports_monitor", "usb_path"}
    with patch("os.listdir",              return_value=["wlan0"]), \
         patch("src.adapters._is_wifi_iface",        return_value=True), \
         patch("src.adapters._get_mac",              return_value="aa:bb:cc:dd:ee:ff"), \
         patch("src.adapters._get_driver",           return_value="rtl8812au"), \
         patch("src.adapters._get_chipset",          return_value="rtl8812au"), \
         patch("src.adapters.check_monitor_support", return_value=True), \
         patch("src.adapters._get_usb_path",         return_value="usb1/1.3"):
        result = list_wifi_adapters()

    assert required.issubset(result[0].keys())


def test_list_returns_sorted_by_iface_name():
    """Адаптеры возвращаются в алфавитном порядке имён интерфейсов."""
    with patch("os.listdir",              return_value=["wlan1", "wlan0"]), \
         patch("src.adapters._is_wifi_iface",        return_value=True), \
         patch("src.adapters._get_mac",              return_value="aa:bb:cc:dd:ee:ff"), \
         patch("src.adapters._get_driver",           return_value="rtl8812au"), \
         patch("src.adapters._get_chipset",          return_value="rtl8812au"), \
         patch("src.adapters.check_monitor_support", return_value=False), \
         patch("src.adapters._get_usb_path",         return_value=None):
        result = list_wifi_adapters()

    assert result[0]["iface"] == "wlan0"
    assert result[1]["iface"] == "wlan1"


def test_list_field_error_yields_adapter_with_defaults():
    """Ошибка при сборке поля → адаптер попадает в список с 'unknown'."""
    with patch("os.listdir",              return_value=["wlan0"]), \
         patch("src.adapters._is_wifi_iface",        return_value=True), \
         patch("src.adapters._get_mac",              side_effect=RuntimeError("boom")), \
         patch("src.adapters._get_driver",           return_value="rtl8812au"), \
         patch("src.adapters._get_chipset",          return_value="rtl8812au"), \
         patch("src.adapters.check_monitor_support", return_value=False), \
         patch("src.adapters._get_usb_path",         return_value=None):
        result = list_wifi_adapters()

    assert len(result) == 1
    assert result[0]["mac"]    == "unknown"
    assert result[0]["iface"]  == "wlan0"     # iface из ключа — сохраняется


def test_list_oserror_on_sys_returns_empty():
    """/sys/class/net недоступен → пустой список, без исключений."""
    with patch("os.listdir", side_effect=OSError("no /sys")):
        result = list_wifi_adapters()
    assert result == []


def test_list_multiple_wifi_adapters():
    """Два Wi-Fi адаптера — оба попадают в результат."""
    with patch("os.listdir",              return_value=["wlan0", "wlan1"]), \
         patch("src.adapters._is_wifi_iface",        return_value=True), \
         patch("src.adapters._get_mac",              side_effect=["aa:bb:cc:dd:ee:ff",
                                                                  "11:22:33:44:55:66"]), \
         patch("src.adapters._get_driver",           return_value="rtl8812au"), \
         patch("src.adapters._get_chipset",          return_value="rtl8812au"), \
         patch("src.adapters.check_monitor_support", return_value=True), \
         patch("src.adapters._get_usb_path",         return_value=None):
        result = list_wifi_adapters()

    assert len(result) == 2
    ifaces = {a["iface"] for a in result}
    assert ifaces == {"wlan0", "wlan1"}