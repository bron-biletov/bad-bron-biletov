"""Основной цикл: логин, периодическая проверка наличия мест, бронирование."""
from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import Page

from . import rw_site
from .models import AppConfig
from .notifier import Notifier
from .rw_site import SiteInteractionError

logger = logging.getLogger("bron_biletov")


class Poller:
    def __init__(self, config: AppConfig, notifier: Notifier) -> None:
        self._config = config
        self._notifier = notifier
        self._consecutive_errors = 0

    async def run(self, page: Page, once: bool = False) -> bool:
        """Запускает цикл опроса. Возвращает True, если билет был забронирован."""
        ok = await rw_site.login(page, self._config.account.login, self._config.account.password)
        if not ok:
            self._notifier.notify_error(
                "Не удалось войти в личный кабинет. Проверьте логин/пароль в config.yaml "
                "и/или откалибруйте селекторы формы входа в rw_site.py."
            )
            return False

        route = self._config.route
        self._notifier.notify_info(
            f"Начинаю мониторинг рейса {route.from_station} -> {route.to_station} "
            f"на {route.date}"
            + (f", поезд {route.train_number}" if route.train_number else "")
        )

        booked = False
        while True:
            try:
                match = await rw_site.find_matching_train(page, route)
                self._consecutive_errors = 0

                if match is None:
                    logger.info("Подходящий рейс не найден на странице результатов")
                elif not match.has_available_seats:
                    logger.info(
                        "Поезд %s (%s) найден, но мест нет",
                        match.train_number,
                        match.departure_time,
                    )
                else:
                    logger.info(
                        "Поезд %s (%s): есть свободные места! Пытаюсь забронировать...",
                        match.train_number,
                        match.departure_time,
                    )
                    success = await rw_site.book_train(
                        page, match, self._config.passenger, car_type=route.car_type
                    )
                    if success:
                        self._notifier.notify_success(
                            f"Билет забронирован! Поезд {match.train_number} "
                            f"({route.from_station} -> {route.to_station}, {route.date}). "
                            "Проверьте личный кабинет на pass.rw.by и завершите оплату "
                            "в отведённое время, если она требуется."
                        )
                        booked = True
                        if self._config.polling.stop_after_first_booking or once:
                            return True
                    else:
                        logger.info(
                            "Не успел забронировать (места разобрали раньше) — продолжаю опрос"
                        )

            except SiteInteractionError as exc:
                self._consecutive_errors += 1
                self._notifier.notify_error(f"Ошибка взаимодействия с сайтом: {exc}")
            except Exception as exc:  # noqa: BLE001
                self._consecutive_errors += 1
                logger.exception("Непредвиденная ошибка во время опроса: %s", exc)

            if once:
                return booked

            await self._sleep_before_next_check()

    async def _sleep_before_next_check(self) -> None:
        polling = self._config.polling
        if self._consecutive_errors >= 3:
            delay = polling.error_backoff_seconds
            logger.warning(
                "%d ошибок подряд — увеличенная пауза %.0f сек.",
                self._consecutive_errors,
                delay,
            )
        else:
            delay = random.uniform(polling.interval_min_seconds, polling.interval_max_seconds)
            logger.debug("Следующая проверка через %.1f сек.", delay)
        await asyncio.sleep(delay)
