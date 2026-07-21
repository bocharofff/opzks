# -*- coding: utf-8 -*-
"""
src/ui.py

Единая точка вывода в консоль для wifi-monitor: общий ``rich.Console`` (его же
использует ``RichHandler`` логирования в ``src.config.setup_logging``, чтобы
таблицы/спиннеры/логи не «дрались» за stdout) и готовые рендереры для типовых
экранов — список адаптеров, результаты сканирования, итог проверки точки,
сводка экспорта, спиннер для дискретных ожиданий.

Модули более низкого уровня (kismet_runner, scanner, gps_monitor, ap_checker)
UI-независимы — этот модуль используется только из src/cli.py.
"""

import logging

from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)

#: Общий Console — используется и здесь, и в src.config.setup_logging (RichHandler)
console = Console()

# Цвета статусов проверки точки оператора (см. src/ap_checker.py: check_one/_try_connect)
_STATUS_STYLES = {
    "ok":              "bold green",
    "captive_portal":  "yellow",
    "eap_unsupported": "yellow",
    "no_assoc":        "bold red",
    "no_dhcp":         "bold red",
    "no_dns":          "bold red",
    "no_inet":         "bold red",
    "timeout":         "bold red",
    "error":           "bold red",
}


def adapters_table(adapters):
    """Таблица Wi-Fi адаптеров (rich-замена adapters.format_adapters_table).

    Args:
        adapters: Список словарей от :func:`src.adapters.list_wifi_adapters`.

    Returns:
        :class:`rich.table.Table`, готовая к ``console.print``.
    """
    table = Table(title="Wi-Fi адаптеры")
    table.add_column("№", justify="right")
    table.add_column("Интерфейс")
    table.add_column("MAC")
    table.add_column("Драйвер/Чипсет")
    table.add_column("Monitor", justify="center")
    table.add_column("USB")

    for i, a in enumerate(adapters, start=1):
        # Если chipset и driver оба известны и различаются — показываем chipset
        if (
            a.get("chipset", "unknown") not in ("unknown", "")
            and a.get("driver", "unknown") not in ("unknown", "")
            and a["chipset"] != a["driver"]
        ):
            driver_chip = a["chipset"]
        else:
            driver_chip = a.get("driver", "unknown")

        monitor_cell = "[bold green]✓[/]" if a.get("supports_monitor") \
            else "[bold red]✗[/]"
        table.add_row(
            str(i),
            a.get("iface", "?"),
            a.get("mac", "unknown"),
            driver_chip,
            monitor_cell,
            a.get("usb_path") or "—",
        )
    return table


def scan_results_table(rows):
    """Таблица результатов сканирования (rich-замена cli._print_scan_results).

    Args:
        rows: Список объектов с доступом по ключу (``sqlite3.Row`` или ``dict``)
              с полями ``bssid, ssid, encryption, channel, lat, lon``.

    Returns:
        :class:`rich.table.Table`.
    """
    table = Table(title="Результаты сканирования (сетей: {})".format(len(rows)))
    table.add_column("BSSID")
    table.add_column("SSID")
    table.add_column("Шифр.")
    table.add_column("Канал", justify="right")
    table.add_column("Широта", justify="right")
    table.add_column("Долгота", justify="right")

    for r in rows:
        lat = r["lat"]
        lon = r["lon"]
        table.add_row(
            r["bssid"] or "",
            (r["ssid"] or "")[:31],
            (r["encryption"] or "")[:9],
            str(r["channel"] or ""),
            "{:.5f}".format(lat) if lat is not None else "No GPS",
            "{:.5f}".format(lon) if lon is not None else "No GPS",
        )
    return table


def ap_result(ap_id, status, rtt_ms=None, ssid=None):
    """Печатает цветной итог проверки одной точки доступа оператора.

    Не участвует в logging — вызывается из ``cli.ap_check_loop`` ДОПОЛНИТЕЛЬНО
    к ``logger.debug`` того же факта (см. вызывающий код), который сохраняет
    полную детальность в файловый лог. Печатается ВСЕГДА (и в обычном, и в
    debug режиме консоли) — это финальный пользовательский результат, а не
    отладочная деталь, которую стоило бы прятать в обычном режиме.

    Args:
        ap_id:  Идентификатор точки.
        status: Статус из ap_checker (``ok``/``no_assoc``/.../``eap_unsupported``).
        rtt_ms: RTT в мс или ``None``.
        ssid:   Имя сети (опционально, для контекста в выводе).
    """
    style = _STATUS_STYLES.get(status, "white")
    rtt = "{:.0f} мс".format(rtt_ms) if rtt_ms else "—"
    label = "{} (ssid={})".format(ap_id, ssid) if ssid else ap_id
    # highlight=False — иначе автоподсветка rich (числа, токены и т.п.) дробит
    # уже раскрашенную строку на лишние ANSI-сегменты внутри одного стиля.
    console.print(
        "[{style}]● {label}: {status}[/{style}] — RTT {rtt}".format(
            style=style, label=label, status=status, rtt=rtt,
        ),
        highlight=False,
    )


def export_summary_table(items):
    """Итоговая таблица экспорта.

    Args:
        items: Список кортежей ``(profile, description, path)`` — описание
               результата в готовом текстовом виде (напр. ``"123 строк"``,
               ``"5 сетей, 8200 сэмплов"``).

    Returns:
        :class:`rich.table.Table`.
    """
    table = Table(title="Экспорт завершён")
    table.add_column("Профиль")
    table.add_column("Результат")
    table.add_column("Путь")

    for profile, description, path in items:
        table.add_row(profile, description, path)
    return table


def spinner(message):
    """Спиннер для дискретных ожиданий (GPS-фикс, запуск Kismet, скан эфира).

    Использование::

        with ui.spinner("Проверяем GPS-фикс..."):
            ok = gps.check_fix()

    В режиме 3 (два потока пишут в консоль параллельно) спиннеры не
    используются — там полагаемся на обычные логи.

    Args:
        message: Текст, отображаемый рядом со спиннером.

    Returns:
        Контекстный менеджер (``console.status``).
    """
    return console.status(message, spinner="dots")
