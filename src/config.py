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

    Args:
        path: Путь к ``settings.yaml``. По умолчанию ``config/settings.yaml``.

    Returns:
        Плоский ``dict`` с итоговыми настройками приложения.
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
    return merged


#: Логгеры сторонних библиотек, которые в DEBUG сыплют посторонним шумом
#: (HTTP-детали соединений и т.п.) — приглушаем их отдельно от нашего кода.
_NOISY_THIRD_PARTY = ("urllib3", "requests", "gpsd")


def setup_logging(config: dict) -> None:
    """Настраивает корневой логгер приложения в двух режимах: обычном и debug.

    Создаёт два handler'а с РАЗНЫМИ уровнями (root всегда ``DEBUG``, чтобы оба
    handler'а получали все записи, а фильтрация — на уровне handler'а):

    * ``StreamHandler`` (консоль) — уровень зависит от режима:
        - обычный режим (``config['debug']`` не задан/``False``) — уровень из
          ``config['log_level']`` (по умолчанию ``INFO``): только майлстоуны,
          итоги проверки точек, warnings и errors;
        - debug-режим (``config['debug'] = True``, обычно через флаг ``--debug``)
          — уровень ``DEBUG``: полная детальность (пер-цикловые синхронизации,
          промежуточные шаги проверки точек и т.д.).
    * ``FileHandler`` (``config['log_file']``) — ВСЕГДА ``DEBUG``, независимо от
      режима консоли: полный журнал доступен для разбора инцидентов постфактум
      без перезапуска в debug-режиме.

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

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(console_level)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    # Избегаем дублирования handler'ов при повторном вызове
    if root.handlers:
        root.handlers.clear()

    # root — DEBUG, чтобы ничего не отсекалось до handler'ов; реальная
    # фильтрация консоли/файла — через уровни самих handler'ов выше.
    root.setLevel(logging.DEBUG)
    root.addHandler(stream_handler)
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
