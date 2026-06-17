"""
src/gps_monitor.py

Получает координаты от gpsd и отдаёт их остальным компонентам системы.
Используется из kismet_runner.py, ap_checker.py и cli.py.

Зависимость: gpsd-py3 (pip install gpsd-py3)
"""

import logging
import threading
from datetime import datetime, timezone

import gpsd

logger = logging.getLogger(__name__)


class GPSMonitor:
    """
    Обёртка над gpsd с фоновым опросом координат.

    Создать → start() → использовать latest() → stop().
    При отсутствии gpsd-сервиса или GPS-фикса система продолжает
    работу: latest() возвращает None, has_fix() возвращает False.
    """

    def __init__(self, host="127.0.0.1", port=2947):
        self._host = host
        self._port = port

        self._latest = None          # последняя известная позиция
        self._running = False        # флаг работы фонового потока
        self._thread = None          # ссылка на фоновый поток
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Управление жизненным циклом
    # ------------------------------------------------------------------

    def start(self):
        """
        Подключается к gpsd и запускает фоновый поток опроса.
        При ошибке подключения логирует ERROR и возвращает управление —
        поток не запускается, latest() будет возвращать None.
        """
        try:
            gpsd.connect(host=self._host, port=self._port)
            logger.info("Подключено к gpsd на %s:%s", self._host, self._port)
        except Exception as exc:
            logger.error("Не удалось подключиться к gpsd: %s", exc)
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="gps-poll",
            daemon=True,
        )
        self._thread.start()
        logger.info("Фоновый опрос GPS запущен")

    def stop(self):
        """
        Останавливает фоновый поток. Ждёт завершения не более 3 секунд.
        """
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                logger.warning("GPS-поток не завершился за 3 секунды")
            else:
                logger.info("GPS-поток остановлен")
        self._thread = None

    # ------------------------------------------------------------------
    # Фоновый цикл опроса
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """
        Раз в секунду запрашивает текущую позицию у gpsd.
        При получении 2D/3D-фикса обновляет _latest.
        Все исключения перехватываются — цикл продолжается.
        """
        import time

        while self._running:
            try:
                packet = gpsd.get_current()

                # mode 2 — 2D-фикс, mode 3 — 3D-фикс (есть высота)
                if packet.mode >= 2:
                    alt = None
                    if packet.mode >= 3:
                        try:
                            alt = float(packet.alt)
                        except (AttributeError, TypeError, ValueError):
                            alt = None

                    position = {
                        "lat": float(packet.lat),
                        "lon": float(packet.lon),
                        "timestamp": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "mode": int(packet.mode),
                        "alt": alt,
                    }
                    with self._lock:
                        self._latest = position

            except Exception as exc:
                logger.warning("Ошибка при опросе gpsd: %s", exc)

            time.sleep(1)

    # ------------------------------------------------------------------
    # Публичные методы получения данных
    # ------------------------------------------------------------------

    def check_fix(self):
        """
        Одноразовая синхронная проверка наличия GPS-фикса.
        Подключается к gpsd, делает один запрос, возвращает True если
        mode >= 2. Не использует и не запускает фоновый поток.
        При любой ошибке возвращает False.
        """
        try:
            gpsd.connect(host=self._host, port=self._port)
            packet = gpsd.get_current()
            return packet.mode >= 2
        except Exception as exc:
            logger.warning("check_fix: ошибка при запросе gpsd: %s", exc)
            return False

    def get_position(self):
        """
        Возвращает копию последней известной позиции (thread-safe).
        Возвращает None если фикса ещё не было с момента start().

        Формат возвращаемого словаря:
          {
            'lat': float,
            'lon': float,
            'timestamp': str,    # ISO8601 UTC, например '2026-06-02T12:00:00Z'
            'mode': int,         # 2 = 2D-фикс, 3 = 3D-фикс
            'alt': float | None, # высота в метрах, только при mode == 3
          }
        """
        with self._lock:
            if self._latest is None:
                return None
            return dict(self._latest)

    def latest(self):
        """
        Псевдоним для get_position().
        Используется из kismet_runner.py и ap_checker.py.
        """
        return self.get_position()

    def has_fix(self):
        """
        Возвращает True если хотя бы один фикс был получен с момента start().
        """
        with self._lock:
            return self._latest is not None


# ---------------------------------------------------------------------------
# Ручная проверка из командной строки
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2947

    monitor = GPSMonitor(host=host, port=port)

    print("Подключаемся к gpsd на {}:{}...".format(host, port))
    monitor.start()

    print("Ожидаем GPS-фикс (5 секунд)...")
    time.sleep(5)

    pos = monitor.latest()
    if pos:
        print("Позиция получена:")
        print("  Широта:    {}".format(pos["lat"]))
        print("  Долгота:   {}".format(pos["lon"]))
        print("  Высота:    {}".format(pos["alt"]))
        print("  Режим:     {}D-фикс".format(pos["mode"]))
        print("  Время:     {}".format(pos["timestamp"]))
    else:
        print("GPS-фикс не получен. Проверьте что gpsd запущен и приёмник подключён.")
        print("  gpsd -N -G /dev/ttyACM0")
        print("  cgps -s  (для проверки фикса)")

    monitor.stop()
