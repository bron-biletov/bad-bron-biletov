r"""Диагностический скрипт: доходит до страницы /ru/order/passengers/ и
сохраняет её разметку + скриншот локально, чтобы можно было откалибровать
LAST_NAME_LABEL_CANDIDATES и соседние константы в rw_site.py по реальной
форме, без передачи логина/пароля кому-либо ещё.

Запуск (из корня проекта, с уже настроенным config.yaml):

    $env:PYTHONPATH = "src"
    .\.venv\Scripts\python.exe scripts\dump_passengers_page.py

Результат сохраняется в debug_passengers_page.html и
debug_passengers_page.png рядом с этим скриптом (в корне проекта).
Эти файлы не содержат пароль, но содержат ваши ФИО/документ/телефон из
config.yaml — не отправляйте их куда-либо, кроме как для локального анализа.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bron_biletov import rw_site  # noqa: E402
from bron_biletov.browser import open_page  # noqa: E402
from bron_biletov.config import load_config  # noqa: E402
from bron_biletov.logging_setup import setup_logging  # noqa: E402


async def main() -> int:
    logger = setup_logging()
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)
    config.browser.headless = False  # чтобы вы видели, что происходит

    async with open_page(config.browser) as page:
        ok = await rw_site.login(page, config.account.login, config.account.password)
        if not ok:
            logger.error("Вход не выполнен — проверьте логин/пароль в config.yaml")
            return 1

        match = await rw_site.find_matching_train(page, config.route)
        if match is None:
            logger.error("Рейс не найден — проверьте route.* в config.yaml")
            return 1
        logger.info("Найден рейс %s (%s), места есть: %s", match.train_number, match.departure_time, match.has_available_seats)

        card = await rw_site._reopen_matched_card(page, match)
        clicked = False
        for text in rw_site.BUY_BUTTON_TEXT_CANDIDATES:
            btn = card.get_by_text(text, exact=False).first
            if await btn.count() > 0:
                await rw_site._click_robust(btn, timeout=8000)
                clicked = True
                break
        if not clicked:
            logger.error("Не удалось нажать кнопку выбора мест — похоже, мест уже нет")
            return 1

        await rw_site.dismiss_known_overlays(page)
        selected = await rw_site._select_car_type_and_carriage(page, config.route.car_type)
        if not selected:
            logger.error("Не удалось выбрать тип вагона/вагон")
            return 1

        await rw_site.dismiss_known_overlays(page)
        entered = await rw_site._click_first_match(
            page, rw_site.ENTER_PASSENGERS_TEXT_CANDIDATES, timeout=10000
        )
        if not entered:
            logger.error("Не удалось найти кнопку 'Ввести данные пассажиров'")
            return 1

        for _ in range(20):
            if rw_site.PASSENGERS_URL_FRAGMENT in page.url:
                break
            await page.wait_for_timeout(500)

        if rw_site.PASSENGERS_URL_FRAGMENT not in page.url:
            logger.error("Не удалось попасть на страницу данных пассажиров (url=%s)", page.url)
            return 1

        out_dir = Path(__file__).resolve().parent.parent
        html_path = out_dir / "debug_passengers_page.html"
        png_path = out_dir / "debug_passengers_page.png"
        html_path.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(png_path), full_page=True)
        logger.info("Сохранено: %s и %s", html_path, png_path)
        logger.info("НЕ бронируйте дальше в этом окне — просто закройте его после проверки.")
        await page.wait_for_timeout(5000)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
