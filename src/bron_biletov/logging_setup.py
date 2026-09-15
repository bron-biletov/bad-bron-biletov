"""Настройка логирования: вывод в консоль и в файл logs/bron_biletov.log."""
from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("bron_biletov")
    if logger.handlers:
        return logger  # уже настроен

    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    try:
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(logs_dir / "bron_biletov.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        # Если не удалось создать файл лога — работаем только с консолью.
        pass

    return logger
