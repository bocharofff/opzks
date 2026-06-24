"""
src/interface_manager.py

Переключает Wi-Fi адаптеры между режимами monitor/managed и управляет
состоянием unmanaged в NetworkManager. Требует root. Не импортирует
ничего из других модулей проекта.
"""

import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

_NM_CONF = "/etc/NetworkManager/conf.d/99-wifi-monitor-unmanaged.conf"


# ---------------------------------------------------------------------------
# Внутренний хелпер
# ---------------------------------------------------------------------------

def _run(cmd, timeout=10):
    """
    Запускает команду через subprocess.
    Возвращает (returncode, stdout, stderr) — никогда не бросает исключений.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.debug("Таймаут (%sс) при выполнении: %s", timeout, " ".join(cmd))
        return -1, "", "timeout"
    except FileNotFoundError:
        logger.debug("Команда не найдена: %s", cmd[0])
        return -1, "", "not found"
    except Exception as exc:
        logger.debug("Неожиданная ошибка при выполнении %s: %s", cmd, exc)
        return -1, "", str(exc)


def _iface_exists(iface):
    """Проверяет наличие интерфейса в sysfs."""
    return os.path.exists("/sys/class/net/{}".format(iface))


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def set_monitor_mode(iface):
    """
    Переводит адаптер iface в monitor mode.

    Сначала пробует airmon-ng, при неудаче — ручной способ через ip/iw.
    Возвращает имя итогового monitor-интерфейса.
    Бросает RuntimeError если оба способа провалились.
    """
    last_error = ""

    # ------------------------------------------------------------------
    # Способ 1: airmon-ng
    # ------------------------------------------------------------------
    rc, stdout, stderr = _run(["airmon-ng", "start", iface], timeout=15)
    if rc == 0:
        # Парсим строку вида "monitor mode vif enabled for [...] on <new_iface>"
        
        match = re.search(r"monitor mode vif enabled.*?on\s+(?:\[.*?\])?([a-zA-Z0-9_.-]+)", stdout)

        if match:
            mon_iface = match.group(1)
            logger.info("airmon-ng: monitor mode включён, новый интерфейс: %s", mon_iface)
            return mon_iface

        # Fallback: проверяем <iface>mon в sysfs
        mon_candidate = iface + "mon"
        if _iface_exists(mon_candidate):
            logger.info("airmon-ng: monitor-интерфейс определён как %s", mon_candidate)
            return mon_candidate

        # airmon-ng отработал успешно, но имя не изменилось (некоторые драйверы)
        if _iface_exists(iface):
            logger.info("airmon-ng: monitor mode включён на %s (имя не изменилось)", iface)
            return iface

        last_error = "airmon-ng завершился с кодом 0, но интерфейс не найден"
        logger.warning("airmon-ng отработал, но результирующий интерфейс не найден; пробуем ручной способ")
    else:
        last_error = stderr.strip() or "airmon-ng вернул {}".format(rc)
        logger.debug("airmon-ng не сработал (%s); пробуем ручной способ", last_error)

    # ------------------------------------------------------------------
    # Способ 2: ручной — ip link + iw
    # ------------------------------------------------------------------
    steps = [
        (["ip", "link", "set", iface, "down"], 5),
        (["iw", "dev", iface, "set", "type", "monitor"], 5),
        (["ip", "link", "set", iface, "up"], 5),
    ]
    for cmd, timeout in steps:
        rc, _, stderr = _run(cmd, timeout=timeout)
        if rc != 0:
            last_error = "{}: {}".format(" ".join(cmd), stderr.strip())
            logger.error("Ручной способ: ошибка на шаге %s — %s", " ".join(cmd), stderr.strip())
            raise RuntimeError(
                "Не удалось включить monitor mode для {}: {}".format(iface, last_error)
            )

    logger.info("Ручной способ: monitor mode включён на %s", iface)
    return iface


def set_managed_mode(iface):
    """
    Возвращает адаптер iface в managed mode.

    Для *mon-интерфейсов пробует airmon-ng stop, затем ручной способ.
    Не бросает исключений — только логирует ошибки.
    """
    if iface.endswith("mon"):
        rc, _, stderr = _run(["airmon-ng", "stop", iface], timeout=15)
        if rc == 0:
            logger.info("airmon-ng: monitor-интерфейс %s остановлен", iface)
            return
        logger.debug("airmon-ng stop не сработал (%s); пробуем ручной способ", stderr.strip())

    # Ручной способ: ip link + iw
    steps = [
        (["ip", "link", "set", iface, "down"], 5),
        (["iw", "dev", iface, "set", "type", "managed"], 5),
        (["ip", "link", "set", iface, "up"], 5),
    ]
    for cmd, timeout in steps:
        rc, _, stderr = _run(cmd, timeout=timeout)
        if rc != 0:
            logger.error(
                "set_managed_mode: ошибка на шаге %s — %s",
                " ".join(cmd), stderr.strip()
            )
            return  # best-effort: не продолжаем сломанную цепочку

    logger.info("Ручной способ: managed mode восстановлен на %s", iface)


def set_unmanaged_networkmanager(iface):
    """
    Запрещает NetworkManager управлять интерфейсом iface.

    Способ 1: nmcli device set managed no.
    Способ 2 (если nmcli недоступен): keyfile + systemctl reload.
    """
    # Способ 1: nmcli
    rc, _, stderr = _run(["nmcli", "device", "set", iface, "managed", "no"], timeout=10)
    if rc == 0:
        logger.info("nmcli: интерфейс %s переведён в unmanaged", iface)
        return
    logger.debug("nmcli не сработал (%s); переходим к keyfile", stderr.strip())

    # Способ 2: keyfile + перезагрузка NM
    content = "[keyfile]\nunmanaged-devices=interface-name:{}\n".format(iface)
    try:
        os.makedirs(os.path.dirname(_NM_CONF), exist_ok=True)
        with open(_NM_CONF, "w") as fh:
            fh.write(content)
        logger.info("Записан unmanaged keyfile: %s", _NM_CONF)
    except OSError as exc:
        logger.error("Не удалось записать keyfile NM: %s", exc)
        return

    rc, _, stderr = _run(["systemctl", "reload", "NetworkManager"], timeout=10)
    if rc != 0:
        logger.error("systemctl reload NetworkManager завершился с ошибкой: %s", stderr.strip())
    else:
        logger.info("NetworkManager перезагружен (keyfile-способ)")


def restore_networkmanager(iface):
    """
    Возвращает NetworkManager управление интерфейсом iface.
    Отменяет действие set_unmanaged_networkmanager. Не бросает исключений.
    """
    # Способ 1: nmcli
    rc, _, stderr = _run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=10)
    if rc == 0:
        logger.info("nmcli: интерфейс %s возвращён в managed", iface)
        return
    logger.debug("nmcli не сработал (%s); удаляем keyfile", stderr.strip())

    # Способ 2: удаление keyfile + перезагрузка NM
    if os.path.exists(_NM_CONF):
        try:
            os.remove(_NM_CONF)
            logger.info("Удалён unmanaged keyfile: %s", _NM_CONF)
        except OSError as exc:
            logger.error("Не удалось удалить keyfile NM: %s", exc)
            return
    else:
        logger.debug("Keyfile NM не найден, удалять нечего")

    rc, _, stderr = _run(["systemctl", "reload", "NetworkManager"], timeout=10)
    if rc != 0:
        logger.error("systemctl reload NetworkManager завершился с ошибкой: %s", stderr.strip())
    else:
        logger.info("NetworkManager перезагружен после удаления keyfile")


# ---------------------------------------------------------------------------
# Запуск из командной строки
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    USAGE = """
Использование (необходим root):

  python -m src.interface_manager monitor <iface>
      Перевести <iface> в monitor mode. Вывести имя результирующего интерфейса.

  python -m src.interface_manager managed <iface>
      Вернуть <iface> в managed mode.

  python -m src.interface_manager unmanage <iface>
      Запретить NetworkManager управлять <iface>.

  python -m src.interface_manager restore <iface>
      Вернуть NetworkManager управление <iface>.

Примеры:
  sudo python -m src.interface_manager monitor wlan0
  sudo python -m src.interface_manager managed wlan0mon
  sudo python -m src.interface_manager unmanage wlan0
  sudo python -m src.interface_manager restore wlan0
""".strip()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 3:
        print(USAGE)
        sys.exit(0)

    action, target_iface = sys.argv[1], sys.argv[2]

    if action == "monitor":
        result = set_monitor_mode(target_iface)
        print("Monitor-интерфейс: {}".format(result))
    elif action == "managed":
        set_managed_mode(target_iface)
    elif action == "unmanage":
        set_unmanaged_networkmanager(target_iface)
    elif action == "restore":
        restore_networkmanager(target_iface)
    else:
        print("Неизвестное действие: {}".format(action))
        print(USAGE)
        sys.exit(1)