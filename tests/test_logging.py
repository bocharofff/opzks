# -*- coding: utf-8 -*-
"""
tests/test_logging.py — режимы логирования (обычный / debug) в src.config.setup_logging.

Проверяем: root всегда DEBUG (чтобы оба handler'а видели все записи), консоль
следует log_level в обычном режиме и поднимается до DEBUG при debug=True, файл
ВСЕГДА пишет DEBUG независимо от режима, повторный вызов не плодит handler'ы,
болтливые сторонние логгеры приглушены до WARNING.
"""

import logging

from src.config import setup_logging, _DEFAULTS


def _handlers_by_type(root):
    stream = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
              and not isinstance(h, logging.FileHandler)]
    file_ = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    return stream, file_


def test_default_is_not_debug():
    assert _DEFAULTS["debug"] is False


def test_normal_mode_console_follows_log_level_file_always_debug(tmp_path):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": False})

    root = logging.getLogger()
    stream, file_ = _handlers_by_type(root)

    assert root.level == logging.DEBUG
    assert len(stream) == 1 and stream[0].level == logging.INFO
    assert len(file_) == 1 and file_[0].level == logging.DEBUG


def test_debug_mode_console_is_debug_file_still_debug(tmp_path):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": True})

    root = logging.getLogger()
    stream, file_ = _handlers_by_type(root)

    assert stream[0].level == logging.DEBUG
    assert file_[0].level == logging.DEBUG


def test_log_level_warning_without_debug_keeps_file_debug(tmp_path):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "WARNING", "log_file": log_file, "debug": False})

    root = logging.getLogger()
    stream, file_ = _handlers_by_type(root)

    assert stream[0].level == logging.WARNING
    assert file_[0].level == logging.DEBUG      # файл не зависит от log_level консоли


def test_debug_true_overrides_log_level_warning(tmp_path):
    # --debug (config['debug']=True) должен поднять консоль до DEBUG, даже если
    # log_level сконфигурирован как WARNING (тише некуда) — это и есть override.
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "WARNING", "log_file": log_file, "debug": True})

    root = logging.getLogger()
    stream, _ = _handlers_by_type(root)
    assert stream[0].level == logging.DEBUG


def test_repeated_calls_do_not_duplicate_handlers(tmp_path):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": False})
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": False})
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": True})

    root = logging.getLogger()
    assert len(root.handlers) == 2      # ровно один StreamHandler + один FileHandler


def test_noisy_third_party_loggers_muted_to_warning(tmp_path):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "INFO", "log_file": log_file, "debug": True})

    for name in ("urllib3", "requests", "gpsd"):
        assert logging.getLogger(name).level == logging.WARNING


def test_unknown_log_level_falls_back_to_info(tmp_path, capsys):
    log_file = str(tmp_path / "app.log")
    setup_logging({"log_level": "NOT_A_LEVEL", "log_file": log_file, "debug": False})

    root = logging.getLogger()
    stream, _ = _handlers_by_type(root)
    assert stream[0].level == logging.INFO
