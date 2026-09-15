"""Уведомление пользователя звуковым сигналом + записью в лог."""
from __future__ import annotations

import logging
import sys
import time

from .models import NotificationConfig

logger = logging.getLogger("bron_biletov")


def _beep_once() -> None:
    """Подаёт один звуковой сигнал, по возможности нативным способом ОС."""
    if sys.platform == "win32":
        try:
            import winsound

            winsound.Beep(1200, 400)
            return
        except Exception:  # pragma: no cover - специфично для окружения
            pass
    # Универсальный запасной вариант — терминальный "звонок" (BEL).
    sys.stdout.write("\a")
    sys.stdout.flush()


class Notifier:
    def __init__(self, config: NotificationConfig) -> None:
        self._config = config

    def _play_sound(self) -> None:
        if not self._config.sound_enabled:
            return
        for _ in range(max(1, self._config.sound_repeats)):
            _beep_once()
            time.sleep(0.3)

    def notify_success(self, message: str) -> None:
        logger.info("УСПЕХ: %s", message)
        print(f"\n{'=' * 60}\n{message}\n{'=' * 60}\n")
        self._play_sound()

    def notify_error(self, message: str) -> None:
        logger.error(message)

    def notify_info(self, message: str) -> None:
        logger.info(message)
