"""
tests/conftest.py — общие pytest-фикстуры для всего тест-сьюта wifi-monitor.

Обнаруживается автоматически через механизм conftest-файлов pytest.
Содержит фикстуры для test_db.py и test_adapters.py.
"""

import pytest

from src.db import init_db


# ---------------------------------------------------------------------------
# Фикстуры для test_db.py
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    """Соединение с временной SQLite-базой, изолированной для каждого теста.

    База создаётся в tmp_path pytest (уникальная директория), поэтому тесты
    не влияют друг на друга. Соединение закрывается после каждого теста.
    """
    db_path = str(tmp_path / "test_wifi.db")
    connection = init_db(db_path)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Фикстуры для test_adapters.py
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_adapters():
    """Список из двух типичных адаптеров для тестов форматирования и поиска.

    wlan0 — USB-адаптер с monitor mode (Alfa AWUS036AC / rtl8812au).
    wlan1 — USB-адаптер без monitor mode (TP-Link / ath9k_htc).
    """
    return [
        {
            "iface":            "wlan0",
            "mac":              "AA:BB:CC:DD:EE:FF",
            "driver":           "rtl8812au",
            "chipset":          "Realtek RTL8812AU",
            "supports_monitor": True,
            "usb_path":         "usb1/1-1.3",
        },
        {
            "iface":            "wlan1",
            "mac":              "11:22:33:44:55:66",
            "driver":           "ath9k_htc",
            "chipset":          "ath9k_htc",
            "supports_monitor": False,
            "usb_path":         "usb1/1.4",
        },
    ]


@pytest.fixture
def single_adapter():
    """Один адаптер с минимальным набором полей — для граничных случаев."""
    return {
        "iface":            "wlan0",
        "mac":              "AA:BB:CC:DD:EE:FF",
        "driver":           "unknown",
        "chipset":          "unknown",
        "supports_monitor": False,
        "usb_path":         None,
    }