"""Управление жизненным циклом браузера Playwright и сохранённой сессией."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .models import BrowserConfig

logger = logging.getLogger("bron_biletov")


@asynccontextmanager
async def open_page(config: BrowserConfig) -> AsyncIterator[Page]:
    """Открывает браузер и страницу, восстанавливая сохранённую сессию (куки),
    если она есть. При выходе из контекста сохраняет актуальную сессию обратно.
    """
    storage_path = Path(config.storage_state_path)
    storage_state = str(storage_path) if storage_path.exists() else None

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(headless=config.headless)
        context: BrowserContext = await browser.new_context(
            storage_state=storage_state,
            locale="ru-RU",
        )
        page: Page = await context.new_page()
        try:
            yield page
        finally:
            try:
                await context.storage_state(path=str(storage_path))
                logger.debug("Сессия браузера сохранена в %s", storage_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось сохранить сессию браузера: %s", exc)
            await context.close()
            await browser.close()
