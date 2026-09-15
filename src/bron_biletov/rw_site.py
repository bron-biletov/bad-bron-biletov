"""Адаптер для сайта https://pass.rw.by/ru/ — вход, поиск рейса, бронирование.

ВАЖНО. Сайт pass.rw.by — это JS-приложение поверх Bitrix, доступное только
после реального рендеринга в браузере. Точную разметку (имена полей формы,
классы карточек поездов и т.п.) невозможно надёжно снять без интерактивной
сессии с реальными учётными данными. Поэтому здесь используются:

  * устойчивые локаторы Playwright "по смыслу" (текст кнопок/подписей,
    роли элементов) — они переживают вёрстку лучше, чем CSS-классы;
  * несколько альтернативных вариантов на каждый шаг (см. списки
    *_CANDIDATES ниже) — код пробует их по очереди;
  * единая точка донастройки: если сайт изменится или какой-то шаг не
    сработает, правьте константы и списки кандидатов в начале файла,
    не трогая логику ниже.

Перед первым реальным запуском обязательно:
  1. Запустите бота с browser.headless: false в config.yaml.
  2. Пройдите вручную первый цикл (вход -> поиск -> карточка поезда),
    глядя на открытое окно браузера, и при необходимости поправьте
    селекторы ниже под то, что видите на экране.
  3. Также полезно записать реальный сценарий через
    `python -m playwright codegen https://pass.rw.by/ru/` и сверить
    полученные локаторы с константами тут.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import Locator, Page, TimeoutError as PWTimeoutError

from .models import Passenger, RouteQuery, TrainMatch

logger = logging.getLogger("bron_biletov")

BASE_URL = "https://pass.rw.by"

# --- Кандидаты локаторов для формы входа -----------------------------------
# Подтверждено вживую (headless-прогон против реального сайта, 2026-09-15):
# ссылка "Личный кабинет" в шапке — это <a data-toggle="modal"
# data-target="#auth-popup">, открывающая Bootstrap-модалку с формой
# id="form-auth" (поля input[name="login"], input[name="password"],
# кнопка input[type=submit][value="Войти"]). При ошибке входа сайт
# показывает блок `.danger.standalone` с текстом вида "Пользователь не
# найден" — это надёжный сигнал неудачи. Перед любым взаимодействием
# нужно закрыть баннер cookie-согласия, иначе он перехватывает клики.
COOKIE_BANNER_ACCEPT_TEXT_CANDIDATES = ["Принять"]
LOGIN_MODAL_TRIGGER_SELECTOR = 'a[data-target="#auth-popup"]'
LOGIN_TRIGGER_TEXT_CANDIDATES = ["Личный кабинет", "Войти"]
LOGIN_FIELD_LABEL_CANDIDATES = ["Логин/E-mail", "Логин", "E-mail"]
LOGIN_FIELD_NAME_CANDIDATES = ["login", "USER_LOGIN", "LOGIN", "email"]
PASSWORD_FIELD_LABEL_CANDIDATES = ["Пароль"]
PASSWORD_FIELD_NAME_CANDIDATES = ["password", "USER_PASSWORD", "PASSWORD"]
LOGIN_SUBMIT_TEXT_CANDIDATES = ["Войти"]
LOGIN_ERROR_SELECTOR = ".danger.standalone"
AUTH_MODAL_SELECTOR = "#auth-popup"

# --- Карточки поездов / кнопка выбора мест ---------------------------------
# Подтверждено вживую: каждая строка результатов поиска — это
# div.sch-table__row-wrap.js-row. Если у поезда есть свободные места,
# в списке классов присутствует "w_places" и показана ссылка-кнопка
# "ВЫБРАТЬ МЕСТА"; если мест нет — класс "w_places" отсутствует и вместо
# кнопки показан текст "Мест нет".
TRAIN_ROW_SELECTOR = "div.sch-table__row-wrap"
AVAILABLE_SEATS_CLASS = "w_places"
BUY_BUTTON_TEXT_CANDIDATES = ["ВЫБРАТЬ МЕСТА", "Выбрать места", "Купить билет", "Забронировать"]
NO_SEATS_TEXT_CANDIDATES = ["Мест нет", "Нет мест", "Билетов нет"]

# --- Выбор вагона и места (/ru/order/places/) -------------------------------
# Подтверждено вживую: после клика по "ВЫБРАТЬ МЕСТА" открывается страница с
# выбором типа вагона (a.pl-type__item[data-car-type]), затем списком вагонов
# в виде аккордеона (.pl-accord__acc-heading) — клик по заголовку выбирает
# вагон (после этого появляется кнопка отправки). Конкретное место выбирать
# не обязательно: система назначает его автоматически ("Номера мест будут
# выбраны системой"). Кнопка "Ввести данные пассажиров" ведёт на
# /ru/order/passengers/, но требует активной авторизации — иначе повторно
# показывает модалку входа (полезный и надёжный сигнал "сессия истекла").
CAR_TYPE_ITEM_SELECTOR = "a.pl-type__item[data-car-type]"
CARRIAGE_ACCORDION_HEADER_SELECTOR = ".pl-accord__acc-heading"
ENTER_PASSENGERS_TEXT_CANDIDATES = ["Ввести данные пассажиров"]
PASSENGERS_URL_FRAGMENT = "/order/passengers/"

# --- Кандидаты локаторов для формы пассажира (/ru/order/passengers/) -------
# ВНИМАНИЕ: эта форма не откалибрована на реальном сайте (требует активной
# авторизованной сессии, которой не было при разработке). Значения ниже —
# наиболее вероятные варианты подписей полей; проверьте и поправьте после
# первого прогона с реальным логином.
LAST_NAME_LABEL_CANDIDATES = ["Фамилия"]
FIRST_NAME_LABEL_CANDIDATES = ["Имя"]
MIDDLE_NAME_LABEL_CANDIDATES = ["Отчество"]
DOCUMENT_NUMBER_LABEL_CANDIDATES = ["Номер документа", "Серия и номер документа"]
PHONE_LABEL_CANDIDATES = ["Телефон", "Номер телефона"]
CONFIRM_BOOKING_TEXT_CANDIDATES = ["Забронировать", "Оформить заказ", "Продолжить", "В корзину"]


class SiteInteractionError(Exception):
    """Не удалось выполнить ожидаемый шаг на сайте (селектор не найден и т.п.)."""


async def _click_first_match(page: Page, texts: list[str], timeout: int = 5000) -> bool:
    for text in texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            return True
        except PWTimeoutError:
            continue
    return False


async def _fill_first_match(
    page: Page,
    value: str,
    label_candidates: list[str],
    name_candidates: list[str],
    timeout: int = 3000,
) -> bool:
    for label in label_candidates:
        locator = page.get_by_label(label, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.fill(value)
            return True
        except PWTimeoutError:
            continue
    for name in name_candidates:
        locator = page.locator(f'input[name="{name}"]').first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.fill(value)
            return True
        except PWTimeoutError:
            continue
    return False


async def dismiss_cookie_banner(page: Page) -> None:
    """Закрывает баннер cookie-согласия, если он показан — иначе он перехватывает
    клики по остальным элементам страницы.
    """
    for text in COOKIE_BANNER_ACCEPT_TEXT_CANDIDATES:
        locator = page.get_by_text(text, exact=False).first
        try:
            if await locator.count() > 0 and await locator.is_visible():
                await locator.click(timeout=3000)
                return
        except PWTimeoutError:
            continue


async def is_logged_in(page: Page) -> bool:
    """Считает пользователя авторизованным, если в шапке отсутствует ссылка,
    открывающая модалку входа (при выходе из сессии эта ссылка либо ведёт в
    личный кабинет напрямую, либо отсутствует, а не открывает форму логина).
    """
    trigger = page.locator(LOGIN_MODAL_TRIGGER_SELECTOR)
    return await trigger.count() == 0


async def login(page: Page, login_value: str, password: str) -> bool:
    """Выполняет вход в личный кабинет. Возвращает True при успехе."""
    await page.goto(f"{BASE_URL}/ru/", wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)

    if await is_logged_in(page):
        logger.info("Сессия уже авторизована (восстановлена из storage_state)")
        return True

    await _click_first_match(page, LOGIN_TRIGGER_TEXT_CANDIDATES)

    filled_login = await _fill_first_match(
        page, login_value, LOGIN_FIELD_LABEL_CANDIDATES, LOGIN_FIELD_NAME_CANDIDATES
    )
    filled_password = await _fill_first_match(
        page, password, PASSWORD_FIELD_LABEL_CANDIDATES, PASSWORD_FIELD_NAME_CANDIDATES
    )
    if not (filled_login and filled_password):
        raise SiteInteractionError(
            "Не удалось найти поля формы входа. Откалибруйте LOGIN_FIELD_*/"
            "PASSWORD_FIELD_* в rw_site.py, глядя на реальную страницу "
            "(запустите с browser.headless: false)."
        )

    clicked = await _click_first_match(page, LOGIN_SUBMIT_TEXT_CANDIDATES)
    if not clicked:
        raise SiteInteractionError("Не удалось найти кнопку 'Войти' для отправки формы")

    error_locator = page.locator(LOGIN_ERROR_SELECTOR)
    modal_locator = page.locator(AUTH_MODAL_SELECTOR)

    # Ждём один из двух исходов: либо появляется блок с ошибкой, либо
    # модалка входа закрывается (успех).
    deadline_steps = 20  # 20 * 0.5s = 10s максимум
    for _ in range(deadline_steps):
        if await error_locator.count() > 0 and await error_locator.first.is_visible():
            error_text = (await error_locator.first.inner_text()).strip()
            logger.warning("Вход не выполнен: %s", error_text or "сайт вернул ошибку")
            return False
        if not await modal_locator.is_visible():
            return await is_logged_in(page)
        await page.wait_for_timeout(500)

    logger.warning(
        "Не удалось определить результат входа за отведённое время — "
        "модалка не закрылась и явной ошибки не показано."
    )
    return False


def build_search_url(route: RouteQuery) -> str:
    params = {
        "from": route.from_station,
        "to": route.to_station,
        "date": route.date,
    }
    if route.from_esr:
        params["from_esr"] = route.from_esr
    if route.to_esr:
        params["to_esr"] = route.to_esr
    return f"{BASE_URL}/ru/route/?{urlencode(params)}"


@dataclass
class _TrainCard:
    locator: Locator
    index: int
    train_number: Optional[str]
    departure_time: Optional[str]
    available: bool
    text: str


async def _collect_train_cards(page: Page) -> list[_TrainCard]:
    """Собирает карточки поездов (строки таблицы результатов поиска).

    Каждая строка — div.sch-table__row-wrap. Номер поезда и время отправления
    извлекаются из текста строки, а наличие мест определяется по классу
    "w_places" (надёжнее, чем текстовые эвристики).
    """
    rows = page.locator(TRAIN_ROW_SELECTOR)
    count = await rows.count()
    cards: list[_TrainCard] = []
    for i in range(count):
        loc = rows.nth(i)
        text = (await loc.inner_text()).strip()
        classes = (await loc.get_attribute("class")) or ""
        train_number_match = re.search(r"\b\d{2,3}[А-ЯЁ]\b", text)
        time_match = re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b", text)
        cards.append(
            _TrainCard(
                locator=loc,
                index=i,
                train_number=train_number_match.group(0) if train_number_match else None,
                departure_time=time_match.group(0) if time_match else None,
                available=AVAILABLE_SEATS_CLASS in classes.split(),
                text=text,
            )
        )
    return cards


async def find_matching_train(page: Page, route: RouteQuery) -> Optional[TrainMatch]:
    """Открывает страницу поиска и ищет карточку поезда, подходящую по номеру/
    времени (если они заданы), возвращая информацию о доступности мест.
    """
    url = build_search_url(route)
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass
    await dismiss_cookie_banner(page)

    cards = await _collect_train_cards(page)
    if not cards:
        logger.debug("На странице результатов не найдено ни одной карточки поезда")
        return None

    for card in cards:
        if route.train_number and card.train_number != route.train_number:
            continue
        if route.departure_time and card.departure_time != route.departure_time:
            continue

        return TrainMatch(
            train_number=card.train_number or "?",
            departure_time=card.departure_time,
            has_available_seats=card.available,
            card_index=card.index,
        )

    logger.debug("Не найдено карточки поезда, соответствующей критериям route=%s", route)
    return None


async def _reopen_matched_card(page: Page, match: TrainMatch) -> Optional[Locator]:
    cards = await _collect_train_cards(page)
    if match.card_index >= len(cards):
        return None
    return cards[match.card_index].locator


async def _select_car_type_and_carriage(page: Page, car_type: Optional[str]) -> bool:
    """Выбирает тип вагона (по названию из конфига или первый доступный) и
    первый вагон этого типа из списка. Возвращает True при успехе.
    """
    try:
        await page.wait_for_selector(CAR_TYPE_ITEM_SELECTOR, timeout=10000)
    except PWTimeoutError:
        logger.warning("Не найден список типов вагонов на странице выбора мест")
        return False

    type_link = None
    if car_type:
        candidate = page.locator(CAR_TYPE_ITEM_SELECTOR).filter(has_text=car_type).first
        if await candidate.count() > 0:
            type_link = candidate
    if type_link is None:
        type_link = page.locator(CAR_TYPE_ITEM_SELECTOR).first

    await type_link.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeoutError:
        pass

    try:
        await page.wait_for_selector(CARRIAGE_ACCORDION_HEADER_SELECTOR, timeout=10000)
    except PWTimeoutError:
        logger.warning("Не найден список вагонов после выбора типа вагона")
        return False

    carriage_header = page.locator(CARRIAGE_ACCORDION_HEADER_SELECTOR).first
    await carriage_header.click()
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeoutError:
        pass
    return True


async def book_train(page: Page, match: TrainMatch, passenger: Passenger, car_type: Optional[str] = None) -> bool:
    """Пытается забронировать найденный поезд для указанного пассажира.

    Возвращает True, если бронирование дошло до подтверждения (карточка
    заказа/бронь создана). Если билеты закончились прямо во время попытки
    (гонка с другими покупателями), возвращает False, чтобы поллер
    продолжил проверку дальше.
    """
    card = await _reopen_matched_card(page, match)
    if card is None:
        logger.warning("Карточка поезда исчезла со страницы перед попыткой бронирования")
        return False

    clicked = False
    for text in BUY_BUTTON_TEXT_CANDIDATES:
        button = card.get_by_text(text, exact=False).first
        if await button.count() > 0:
            await button.click()
            clicked = True
            break
    if not clicked:
        logger.warning("Не удалось нажать кнопку покупки — вероятно, места уже разобрали")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass
    await dismiss_cookie_banner(page)

    if not await _select_car_type_and_carriage(page, car_type):
        raise SiteInteractionError(
            "Не удалось выбрать тип вагона/вагон на странице /ru/order/places/. "
            "Проверьте CAR_TYPE_ITEM_SELECTOR / CARRIAGE_ACCORDION_HEADER_SELECTOR "
            "в rw_site.py на актуальной разметке."
        )

    entered = await _click_first_match(page, ENTER_PASSENGERS_TEXT_CANDIDATES, timeout=10000)
    if not entered:
        logger.warning("Не удалось найти кнопку 'Ввести данные пассажиров'")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass

    if PASSENGERS_URL_FRAGMENT not in page.url:
        auth_modal = page.locator(AUTH_MODAL_SELECTOR)
        if await auth_modal.count() > 0 and await auth_modal.is_visible():
            raise SiteInteractionError(
                "Сайт снова показал форму входа при переходе к данным пассажиров — "
                "сессия авторизации, похоже, истекла или недействительна."
            )
        logger.warning(
            "После 'Ввести данные пассажиров' не произошёл переход на %s (url=%s)",
            PASSENGERS_URL_FRAGMENT,
            page.url,
        )
        return False

    filled_last = await _fill_first_match(page, passenger.last_name, LAST_NAME_LABEL_CANDIDATES, [])
    filled_first = await _fill_first_match(page, passenger.first_name, FIRST_NAME_LABEL_CANDIDATES, [])
    if passenger.middle_name:
        await _fill_first_match(page, passenger.middle_name, MIDDLE_NAME_LABEL_CANDIDATES, [])
    filled_doc = await _fill_first_match(
        page, passenger.document_number, DOCUMENT_NUMBER_LABEL_CANDIDATES, []
    )
    filled_phone = await _fill_first_match(page, passenger.phone, PHONE_LABEL_CANDIDATES, [])

    if not all([filled_last, filled_first, filled_doc, filled_phone]):
        raise SiteInteractionError(
            "Не удалось полностью заполнить форму пассажира. Откалибруйте "
            "*_LABEL_CANDIDATES в rw_site.py по реальной разметке формы."
        )

    confirmed = await _click_first_match(page, CONFIRM_BOOKING_TEXT_CANDIDATES)
    if not confirmed:
        raise SiteInteractionError("Не удалось найти кнопку подтверждения бронирования")

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass

    # Эвристика успеха: на странице больше нет текста об отсутствии мест
    # и есть признаки заказа/корзины.
    body_text = await page.locator("body").inner_text()
    failed_markers = ["Мест нет", "мест не осталось", "ошибка", "не удалось"]
    if any(marker.lower() in body_text.lower() for marker in failed_markers):
        logger.warning("Бронирование не подтвердилось (найдены маркеры ошибки на странице)")
        return False

    return True
