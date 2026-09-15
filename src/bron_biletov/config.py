"""Загрузка и валидация конфигурации из YAML-файла + переменных окружения."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .models import (
    Account,
    AppConfig,
    BrowserConfig,
    NotificationConfig,
    Passenger,
    PollingConfig,
    RouteQuery,
)


class ConfigError(Exception):
    """Ошибка конфигурации: отсутствуют обязательные поля или файл не найден."""


def _require(d: Dict[str, Any], key: str, context: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"В секции '{context}' отсутствует обязательное поле '{key}'")
    return d[key]


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _optional_str(d: Dict[str, Any], key: str, context: str) -> Optional[str]:
    """Достаёт необязательное строковое поле, приводя число к строке.

    Нужно из-за того, что YAML 1.1 разбирает значения без кавычек как числа —
    например, `train_number: 876Щ` без кавычек ещё сойдёт за строку (в ней
    есть буква), но `departure_time: 19:38` без кавычек PyYAML понимает как
    шестидесятеричное число 1178, а не как строку "19:38". Без явного
    приведения к str такие значения потом никогда не совпадают с текстом,
    считанным со страницы, и поиск рейса молча не находит ничего подходящего.
    """
    value = d.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(
            f"Поле '{context}.{key}' задано без кавычек, из-за чего YAML прочитал его "
            f"как число ({value!r}), а не как строку — вероятно, значение будет "
            f'никогда не совпадать с данными сайта. Возьмите значение в кавычки, '
            f'например: {key}: "19:38"'
        )
    return value


def _validate_time_format(value: Optional[str], context: str, key: str) -> Optional[str]:
    if value is None:
        return None
    if not _TIME_RE.match(value):
        raise ConfigError(
            f"Поле '{context}.{key}' должно быть в формате ЧЧ:ММ (например \"19:38\"), "
            f"получено {value!r}"
        )
    return value


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Файл конфигурации не найден: {path}. "
            "Скопируйте config.example.yaml в config.yaml и заполните его."
        )

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    account_raw = raw.get("account", {})
    login = os.environ.get("RW_LOGIN") or _require(account_raw, "login", "account")
    password = os.environ.get("RW_PASSWORD") or _require(account_raw, "password", "account")
    account = Account(login=login, password=password)

    passenger_raw = raw.get("passenger", {})
    passenger = Passenger(
        last_name=_require(passenger_raw, "last_name", "passenger"),
        first_name=_require(passenger_raw, "first_name", "passenger"),
        middle_name=passenger_raw.get("middle_name"),
        document_type=_require(passenger_raw, "document_type", "passenger"),
        document_number=_require(passenger_raw, "document_number", "passenger"),
        phone=_require(passenger_raw, "phone", "passenger"),
    )

    route_raw = raw.get("route", {})
    departure_time = _validate_time_format(
        _optional_str(route_raw, "departure_time", "route"), "route", "departure_time"
    )
    route = RouteQuery(
        from_station=_require(route_raw, "from_station", "route"),
        to_station=_require(route_raw, "to_station", "route"),
        date=_require(route_raw, "date", "route"),
        from_esr=str(route_raw["from_esr"]) if route_raw.get("from_esr") else None,
        to_esr=str(route_raw["to_esr"]) if route_raw.get("to_esr") else None,
        train_number=_optional_str(route_raw, "train_number", "route"),
        departure_time=departure_time,
        car_type=route_raw.get("car_type"),
    )

    polling_raw = raw.get("polling", {})
    polling = PollingConfig(
        interval_min_seconds=float(polling_raw.get("interval_min_seconds", 15)),
        interval_max_seconds=float(polling_raw.get("interval_max_seconds", 30)),
        error_backoff_seconds=float(polling_raw.get("error_backoff_seconds", 120)),
        stop_after_first_booking=bool(polling_raw.get("stop_after_first_booking", True)),
    )
    if polling.interval_min_seconds <= 0 or polling.interval_max_seconds < polling.interval_min_seconds:
        raise ConfigError(
            "Некорректный диапазон polling.interval_min_seconds / interval_max_seconds"
        )

    browser_raw = raw.get("browser", {})
    browser = BrowserConfig(
        headless=bool(browser_raw.get("headless", False)),
        storage_state_path=browser_raw.get("storage_state_path", "storage_state.json"),
    )

    notifications_raw = raw.get("notifications", {})
    notifications = NotificationConfig(
        sound_enabled=bool(notifications_raw.get("sound_enabled", True)),
        sound_repeats=int(notifications_raw.get("sound_repeats", 5)),
    )

    return AppConfig(
        account=account,
        passenger=passenger,
        route=route,
        polling=polling,
        browser=browser,
        notifications=notifications,
    )
