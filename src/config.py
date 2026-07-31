"""
src/config.py — загрузка настроек, логирование, валидация прав.

Первый модуль, импортируемый всеми остальными компонентами wifi-monitor.
"""

import logging
import os
import stat
from collections import ChainMap
from pathlib import Path
from typing import Any

import yaml
from rich.logging import RichHandler

from src.ui import console as _console

# ---------------------------------------------------------------------------
# Встроенные значения по умолчанию (зеркалируют config/settings.yaml)
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    "db_path":           "data/wifi_monitor.db",
    "kismet_db_path":    "data/kismet.kismet",
    "targets_path":      "config/ap_targets.yaml",
    "gps_host":          "127.0.0.1",
    "gps_port":          2947,
    "sync_interval_sec": 30,
    "log_level":         "INFO",
    "log_file":          "data/wifi_monitor.log",
    # Обычный режим (по умолчанию): консоль показывает только майлстоуны, итоги
    # проверки точек и warnings/errors (уровень — log_level). Файл всегда пишет
    # полный DEBUG. debug=true (или флаг --debug) поднимает консоль до DEBUG.
    "debug":             False,
    # Тепловая карта: даунсэмплинг сэмплов сигнала (1 сэмпл на N секунд на сеть)
    "heatmap_sample_sec": 1,
    # Проверка точек «по присутствию» (разделы плана про режимы 2/3)
    "presence_window_sec": 45,   # окно «видно сейчас» для режима 3 (по монитору)
    "recheck_interval_sec": 60,  # кулдаун перепроверки одной и той же точки
    "scan_interval_sec": 8,      # пауза между сканами/циклами присутствия
    "min_signal_dbm": None,      # опц. порог: слишком слабые точки не проверять
    "default_security": "wpa2-psk",  # запасной тип шифрования, если не выведен из эфира
    # Профиль экспорта "map" — интерактивная HTML-карта (src/wifi_heatmap).
    # Тюнинг живёт только здесь (в CLI отдельных флагов нет): параметров ~20,
    # они редко меняются от запуска к запуску. Значения — дефолты модуля.
    "map": {
        # Отбор сетей
        "top": 10,                  # сколько сетей показать (по убыванию числа замеров)
        "min_points": 20,           # минимум замеров, иначе слой не строится
        # Интерполяция (IDW)
        "grid_step_m": 3.0,         # шаг сетки, метры
        "idw_power": 2.0,           # степень IDW
        "idw_neighbors": 12,        # число ближайших соседей в расчёте
        "idw_radius_m": None,       # радиус поиска соседей (null = без ограничения)
        "max_distance_m": 10.0,     # полуширина зоны «есть данные», метры
        "mask_mode": "track",       # track — коридор вдоль маршрута; points — буфер вокруг точек
        "bridge_max_m": 40.0,       # макс. длина пути между детекциями для склейки коридора
        "bridge_max_s": 30.0,       # макс. пауза между детекциями для склейки коридора
        "cluster_eps_m": 100.0,     # порог пространственной кластеризации сети
        "min_cluster_points": 3,    # минимум точек в кластере, иначе он считается шумом
        "max_cells": 4_000_000,     # предел ячеек сетки на слой (иначе шаг огрубляется)
        # Отрисовка
        "rssi_min": -90.0,          # нижняя граница цветовой шкалы, дБм
        "rssi_max": -30.0,          # верхняя граница цветовой шкалы, дБм
        "rssi_auto": False,         # подгонять шкалу каждого слоя под факт. min/max сети
        "opacity": 0.6,             # прозрачность тепловых слоёв, 0..1
        "cmap": "RdYlGn",           # цветовая карта matplotlib
        "reverse_cmap": True,       # true — красный сильный / зелёный слабый
        "show_points": False,       # добавить слой фактических точек замеров
    },
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def load_config(path: str = "config/settings.yaml") -> dict:
    """Загружает конфигурацию из YAML-файла и объединяет с дефолтами.

    Использует :class:`collections.ChainMap`: значения из файла имеют
    приоритет над встроенными дефолтами. Если файл не найден или не
    является корректным YAML — возвращает только дефолты, предварительно
    сообщив об этом через ``logging.warning``.

    Секции-словари (напр. ``map``) сливаются ПОКЛЮЧЕВО: ChainMap подменяет
    значение целиком, поэтому без отдельного слияния оператор, задавший в
    settings.yaml один параметр карты, потерял бы все остальные её дефолты.

    Args:
        path: Путь к ``settings.yaml``. По умолчанию ``config/settings.yaml``.

    Returns:
        ``dict`` с итоговыми настройками приложения.
    """
    file_values: dict[str, Any] = {}

    config_path = Path(path)
    if not config_path.exists():
        # Логировать пока нельзя — logging ещё не настроен; используем print,
        # чтобы сообщение всё равно дошло до оператора.
        print(
            f"[config] Файл настроек не найден: {path!r} — "
            "используются встроенные дефолты."
        )
    else:
        try:
            with config_path.open(encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                file_values = loaded
            else:
                print(
                    f"[config] {path!r} не содержит YAML-словаря — "
                    "используются встроенные дефолты."
                )
        except yaml.YAMLError as exc:
            print(f"[config] Ошибка парсинга {path!r}: {exc} — используются дефолты.")

    # ChainMap: первый словарь — приоритетный
    merged = dict(ChainMap(file_values, _DEFAULTS))

    # Слияние вложенных секций по ключам: задать в YAML один параметр карты
    # и не потерять остальные дефолты.
    for key, default_value in _DEFAULTS.items():
        if isinstance(default_value, dict):
            file_section = file_values.get(key)
            section = dict(default_value)
            if isinstance(file_section, dict):
                section.update(file_section)
            merged[key] = section

    return merged


#: Логгеры сторонних библиотек, которые в DEBUG сыплют посторонним шумом
#: (HTTP-детали соединений и т.п.) — приглушаем их отдельно от нашего кода.
_NOISY_THIRD_PARTY = ("urllib3", "requests", "gpsd")


def setup_logging(config: dict) -> None:
    """Настраивает корневой логгер приложения в двух режимах: обычном и debug.

    Создаёт два handler'а с РАЗНЫМИ уровнями (root всегда ``DEBUG``, чтобы оба
    handler'а получали все записи, а фильтрация — на уровне handler'а):

    * ``RichHandler`` (консоль, использует общий ``src.ui.console`` — тот же
      Console, которым таблицы/спиннеры пользуются в cli.py, чтобы вывод не
      «дрался» за stdout) — уровень зависит от режима:
        - обычный режим (``config['debug']`` не задан/``False``) — уровень из
          ``config['log_level']`` (по умолчанию ``INFO``): только майлстоуны,
          итоги проверки точек, warnings и errors;
        - debug-режим (``config['debug'] = True``, обычно через флаг ``--debug``)
          — уровень ``DEBUG``: полная детальность (пер-цикловые синхронизации,
          промежуточные шаги проверки точек и т.д.).
    * ``FileHandler`` (``config['log_file']``) — ВСЕГДА ``DEBUG``, независимо от
      режима консоли, обычным (не-rich) форматтером: полный журнал доступен для
      разбора инцидентов постфактум без перезапуска в debug-режиме.

    Дополнительно приглушает известные болтливые сторонние логгеры (см.
    :data:`_NOISY_THIRD_PARTY`) до ``WARNING`` — иначе их DEBUG заливает и
    debug-консоль, и (всегда подробный) файл.

    Args:
        config: Словарь настроек, возвращённый :func:`load_config`. Ключи:
            ``log_level``, ``log_file``, ``debug``.
    """
    log_file: str = config.get("log_file", _DEFAULTS["log_file"])
    log_level_str: str = str(config.get("log_level", _DEFAULTS["log_level"])).upper()
    debug: bool = bool(config.get("debug", _DEFAULTS["debug"]))

    numeric_level = getattr(logging, log_level_str, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
        print(
            f"[config] Неизвестный log_level={log_level_str!r}, "
            "используется INFO."
        )

    console_level = logging.DEBUG if debug else numeric_level

    # Создаём директорию для лог-файла если не существует
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    # RichHandler сам рисует время/уровень своей колонкой — форматтеру оставляем
    # только имя логгера и сообщение, иначе они задвоятся.
    console_handler = RichHandler(
        console=_console,
        show_path=False,
        rich_tracebacks=True,
        log_time_format="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    console_handler.setLevel(console_level)

    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(file_fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    # Избегаем дублирования handler'ов при повторном вызове
    if root.handlers:
        root.handlers.clear()

    # root — DEBUG, чтобы ничего не отсекалось до handler'ов; реальная
    # фильтрация консоли/файла — через уровни самих handler'ов выше.
    root.setLevel(logging.DEBUG)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    for name in _NOISY_THIRD_PARTY:
        logging.getLogger(name).setLevel(logging.WARNING)

    logger.debug(
        "Логирование настроено: debug=%s, консоль=%s, файл=%s (DEBUG)",
        debug, logging.getLevelName(console_level), log_file,
    )


def validate_targets_permissions(path: str) -> bool:
    """Проверяет, что файл с паролями точек доступа имеет права 600.

    Читает биты прав доступа через :func:`os.stat` и сверяет их с
    ``0o600`` (``rw-------``).  Никаких исключений не поднимает —
    все аномалии фиксируются через ``logging.warning``.

    Args:
        path: Путь к ``ap_targets.yaml`` (или другому файлу с секретами).

    Returns:
        ``True``  — файл существует и имеет ровно права ``600``.
        ``False`` — файл отсутствует или права отличаются от ``600``.
    """
    target = Path(path)

    if not target.exists():
        logger.warning(
            "Файл с целевыми точками не найден: %s", path
        )
        return False

    try:
        file_stat = os.stat(target)
    except OSError as exc:
        logger.warning("Не удалось прочитать права файла %s: %s", path, exc)
        return False

    # Оставляем только биты rwxrwxrwx (маска 0o777)
    mode = stat.S_IMODE(file_stat.st_mode)

    if mode != 0o600:
        logger.warning(
            "ap_targets.yaml имеет права %s, рекомендуется 600",
            oct(mode),
        )
        return False

    logger.debug("Права доступа к %s корректны (600).", path)
    return True


# ---------------------------------------------------------------------------
# Демонстрационный запуск
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    cfg = load_config()          # загружаем (или дефолты, если нет файла)
    setup_logging(cfg)           # настраиваем логирование

    logger.info("Конфигурация загружена успешно.")

    targets_ok = validate_targets_permissions(cfg["targets_path"])
    logger.info(
        "Проверка прав %s: %s",
        cfg["targets_path"],
        "OK" if targets_ok else "WARN",
    )

    print("\n── Итоговый конфиг ──────────────────────────────")
    pprint.pprint(cfg, sort_dicts=False)
    print("─────────────────────────────────────────────────")
