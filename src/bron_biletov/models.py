"""Типизированные модели конфигурации робота Bron Biletov."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    login: str
    password: str


@dataclass
class Passenger:
    last_name: str
    first_name: str
    middle_name: Optional[str]
    document_type: str
    document_number: str
    phone: str

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)


@dataclass
class RouteQuery:
    from_station: str
    to_station: str
    date: str
    from_esr: Optional[str] = None
    to_esr: Optional[str] = None
    train_number: Optional[str] = None
    departure_time: Optional[str] = None
    car_type: Optional[str] = None


@dataclass
class PollingConfig:
    interval_min_seconds: float = 15.0
    interval_max_seconds: float = 30.0
    error_backoff_seconds: float = 120.0
    stop_after_first_booking: bool = True


@dataclass
class BrowserConfig:
    headless: bool = False
    storage_state_path: str = "storage_state.json"


@dataclass
class NotificationConfig:
    sound_enabled: bool = True
    sound_repeats: int = 5


@dataclass
class AppConfig:
    account: Account
    passenger: Passenger
    route: RouteQuery
    polling: PollingConfig = field(default_factory=PollingConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


@dataclass
class TrainMatch:
    """Найденный на странице результатов поиска поезд, подходящий под критерии."""

    train_number: str
    departure_time: Optional[str]
    has_available_seats: bool
    # Локатор на кнопку/ссылку "Купить билет" / "Забронировать" внутри карточки поезда.
    # Хранится не сам объект локатора (он живёт только в рамках страницы Playwright),
    # а достаточно данных, чтобы поллер повторно нашёл эту карточку на странице.
    card_index: int
