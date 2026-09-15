"""Точка входа: python -m bron_biletov [--config config.yaml] [--once] [--headed]"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .browser import open_page
from .config import ConfigError, load_config
from .logging_setup import setup_logging
from .notifier import Notifier
from .poller import Poller


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bron Biletov — робот для мониторинга и бронирования "
        "освободившихся билетов на pass.rw.by"
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Путь к файлу конфигурации (по умолчанию config.yaml)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Выполнить только одну проверку и завершиться (удобно для отладки/калибровки)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Принудительно показать окно браузера, даже если в конфиге headless: true",
    )
    return parser.parse_args(argv)


async def _main_async(argv: list[str]) -> int:
    logger = setup_logging()
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error(str(exc))
        return 1

    if args.headed:
        config.browser.headless = False

    notifier = Notifier(config.notifications)

    async with open_page(config.browser) as page:
        poller = Poller(config, notifier)
        booked = await poller.run(page, once=args.once)

    return 0 if booked or not args.once else 2


def main() -> None:
    exit_code = asyncio.run(_main_async(sys.argv[1:]))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
