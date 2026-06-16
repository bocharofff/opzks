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


def setup_logging(config: dict) -> None:
    """Настраивает корневой логгер приложения.

    Создаёт два handler'а:
    * ``StreamHandler`` — вывод в консоль (stderr);
    * ``FileHandler``   — запись в файл из ``config['log_file']``.

    Уровень логирования берётся из ``config['log_level']`` (строка вида
    ``"INFO"``, ``"DEBUG"`` и т.д.); при некорректном значении используется
    ``INFO``.  Родительская директория для лог-файла создаётся автоматически.

    Args:
        config: Словарь настроек, возвращённый :func:`load_config`.
    """
    log_file: str = config.get("log_file", _DEFAULTS["log_file"])
    log_level_str: str = str(config.get("log_level", _DEFAULTS["log_level"])).upper()

    numeric_level = getattr(logging, log_level_str, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
        print(
            f"[config] Неизвестный log_level={log_level_str!r}, "
            "используется INFO."
        )

    # Создаём директорию для лог-файла если не существует
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    # Избегаем дублирования handler'ов при повторном вызове
    if root.handlers:
        root.handlers.clear()

    root.setLevel(numeric_level)
    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    logger.debug("Логирование настроено: level=%s, file=%s", log_level_str, log_file)


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
