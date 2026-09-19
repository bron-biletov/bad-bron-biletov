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
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from playwright.async_api import Locator, Page, TimeoutError as PWTimeoutError

from .models import Passenger, RouteQuery, TrainMatch

logger = logging.getLogger("bron_biletov")

BASE_URL = "https://pass.rw.by"


async def _dump_debug_state(page: Page, label: str) -> None:
    """Сохраняет HTML и скриншот текущей страницы при сбое шага бронирования.

    Это единственный способ разобрать причину сбоя без ручного повторения
    диагностики: следующий провал сам оставит снимок состояния страницы —
    достаточно прочитать debug_<label>_*.html/.png в корне проекта.
    Никогда не выбрасывает исключение — диагностика не должна ронять бота.
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = Path(f"debug_{label}_{timestamp}")
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        logger.info("Сохранён диагностический снимок: %s.html / %s.png", base, base)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось сохранить диагностический снимок: %s", exc)

# --- Кандидаты локаторов для формы входа -----------------------------------
# Подтверждено вживую (headless-прогон против реального сайта, 2026-09-15):
# ссылка "Личный кабинет" в шапке — это <a data-toggle="modal"
# data-target="#auth-popup">, открывающая Bootstrap-модалку с формой
# id="form-auth" (поля input[name="login"], input[name="password"],
# кнопка input[type=submit][value="Войти"]). При ошибке входа сайт
# показывает блок `.danger.standalone` с текстом вида "Пользователь не
# найден" — это надёжный сигнал неудачи. Перед любым взаимодействием
# нужно закрыть баннер cookie-согласия, иначе он перехватывает клики.
# Сайт периодически показывает разные блокирующие Bootstrap-модалки после
# пауз в активности — подтверждено как минимум для "Результаты поиска могли
# устареть" (#timeout-popup) на странице результатов; есть и другие,
# например про обрыв соединения с backend продажи билетов на странице
# выбора мест — их id не откалиброван. Вместо перечисления каждой по имени
# closing-логика в dismiss_unexpected_modals закрывает любую видимую
# модалку, кроме id из ALLOWED_OPEN_MODAL_IDS (то, что мы держим открытым
# намеренно, например форму входа).
ALLOWED_OPEN_MODAL_IDS = {"auth-popup"}
GENERIC_MODAL_CLOSE_SELECTOR = 'button.close, [data-dismiss="modal"]'

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
# Bootstrap-аккордеон добавляет класс "in" раскрытой панели после анимации
# (см. n.prototype.show в app.min.js — collapse-плагин Bootstrap 3).
CARRIAGE_ACCORDION_EXPANDED_SELECTOR = ".pl-accord__collapse.in"
ENTER_PASSENGERS_TEXT_CANDIDATES = ["Ввести данные пассажиров"]
PASSENGERS_URL_FRAGMENT = "/order/passengers/"

# --- Кандидаты локаторов для формы пассажира (/ru/order/passengers/) -------
# Подтверждено вживую (дамп реальной страницы, 2026-09-16): форма для
# пассажира №1 использует поля с суффиксом "_1" — last_name_1, first_name_1,
# middle_name_1, document_type_1 (<select>), document_number_1. Телефона на
# этой странице нет вообще (поле "Телефон" на странице — это форма другого
# виджета поддержки, не пассажира), поэтому passenger.phone здесь не
# используется. Кнопка "Оформить заказ" по умолчанию disabled и включается
# JS-валидацией только после заполнения всех обязательных полей и отметки
# чекбокса согласия с правилами (name="agreement").
LAST_NAME_LABEL_CANDIDATES = ["Фамилия"]
LAST_NAME_NAME_CANDIDATES = ["last_name_1"]
FIRST_NAME_LABEL_CANDIDATES = ["Имя"]
FIRST_NAME_NAME_CANDIDATES = ["first_name_1"]
MIDDLE_NAME_LABEL_CANDIDATES = ["Отчество"]
MIDDLE_NAME_NAME_CANDIDATES = ["middle_name_1"]
DOCUMENT_NUMBER_LABEL_CANDIDATES = ["Номер документа", "Серия и номер документа"]
DOCUMENT_NUMBER_NAME_CANDIDATES = ["document_number_1"]
CONFIRM_BOOKING_TEXT_CANDIDATES = ["Оформить заказ", "Забронировать", "Продолжить", "В корзину"]

# Выпадающий список "Тип документа" — реальные значения option[value] сняты
# со страницы (двухбуквенные коды БЖД). Ключи слева — то, что можно писать
# в config.yaml (passenger.document_type); можно также указать код БЖД
# напрямую (например "ИК") или подстроку видимого названия документа.
DOCUMENT_TYPE_SELECT_NAME_CANDIDATES = ["document_type_1"]
DOCUMENT_TYPE_VALUE_MAP = {
    "passport": "ПБ",  # Паспорт гражданина Республики Беларусь
    "id_card": "ИК",  # Идентификационная карта гражданина РБ
    "birth_certificate": "СР",  # Свидетельство о рождении для граждан РБ
    "residence_permit": "ВЖ",  # Вид на жительство
    "foreign_passport": "ЗЗ",  # Иностранный паспорт
    "driver_license": "ВУ",  # Водительское удостоверение
    "refugee_certificate": "УБ",  # Удостоверение беженца
    "military_id": "ВБ",  # Военный билет
}
AGREEMENT_CHECKBOX_SELECTOR = 'input[type="checkbox"][name="agreement"]'


class SiteInteractionError(Exception):
    """Не удалось выполнить ожидаемый шаг на сайте (селектор не найден и т.п.)."""


async def _click_robust(locator: Locator, timeout: int = 5000) -> bool:
    """Кликает по локатору с запасным вариантом через force=True.

    На страницах заказа снизу есть липкая панель (`class="carriage-cost
    ... fixed"`) с ценой и кнопкой перехода дальше. Если обычный целевой
    элемент после автоскролла Playwright оказывается визуально под этой
    липкой панелью, панель "перехватывает" клик (intercepts pointer events),
    и Playwright бесконечно повторяет попытку со скроллом — это выглядит
    как хаотичное скроллирование страницы вверх-вниз без видимого эффекта.
    force=True кликает по элементу напрямую, минуя проверку перекрытия.
    """
    try:
        await locator.click(timeout=timeout)
        return True
    except PWTimeoutError:
        try:
            await locator.click(timeout=timeout, force=True)
            return True
        except PWTimeoutError:
            return False


async def _click_first_match(page: Page, texts: list[str], timeout: int = 5000) -> bool:
    for text in texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
        except PWTimeoutError:
            continue
        if await _click_robust(locator, timeout=timeout):
            return True
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


async def _select_document_type(page: Page, document_type: str) -> bool:
    """Выбирает тип документа в <select name="document_type_1">.

    Принимает либо ключ из DOCUMENT_TYPE_VALUE_MAP (например "id_card"),
    либо код БЖД напрямую (например "ИК"), либо подстроку видимого названия
    документа (например "идентификационная").
    """
    select_locator = None
    for name in DOCUMENT_TYPE_SELECT_NAME_CANDIDATES:
        loc = page.locator(f'select[name="{name}"]').first
        if await loc.count() > 0:
            select_locator = loc
            break
    if select_locator is None:
        return False

    target = document_type.strip()
    target_value = DOCUMENT_TYPE_VALUE_MAP.get(target.lower(), target)

    options = select_locator.locator("option")
    count = await options.count()
    for i in range(count):
        option = options.nth(i)
        value = await option.get_attribute("value") or ""
        text = (await option.inner_text()).strip()
        if value == target_value or text.lower() == target_value.lower() or target.lower() in text.lower():
            try:
                await select_locator.select_option(value=value)
                return True
            except PWTimeoutError:
                return False
    return False


async def _check_agreement_checkbox(page: Page) -> bool:
    """Отмечает чекбокс согласия с правилами.

    Подтверждено вживую (дамп debug_passenger_form_*): чекбокс оформлен
    через виджет jQuery formstyler — реальный `<input>` обёрнут в
    `<div class="jq-checkbox ..."><input .../><div class="jq-checkbox__div">
    </div></div>`, и именно эта обёртка визуально кликабельна (то, что видит
    и по чему кликает настоящий пользователь). Прямое присвоение
    `input.checked` через JS не работает надёжно, так как formstyler
    отслеживает клики по обёртке и сам управляет синхронизацией состояния —
    его внутренняя модель не в курсе внешнего изменения. Поэтому кликаем по
    обёртке, как это делает браузер при реальном клике, а не по `<input>`.
    """
    checkbox = page.locator(AGREEMENT_CHECKBOX_SELECTOR).first
    if await checkbox.count() == 0:
        return False
    try:
        if await checkbox.is_checked():
            return True
    except PWTimeoutError:
        pass

    wrapper = page.locator("div.jq-checkbox").filter(
        has=page.locator(AGREEMENT_CHECKBOX_SELECTOR)
    ).first
    if await wrapper.count() > 0:
        try:
            # Обычный клик по координатам почти наверняка попадёт в сам
            # <input> — он визуально скрыт (opacity:0), но по факту лежит
            # поверх обёртки в том же месте. Это вызовет ОДНОВРЕМЕННО и
            # нативный тоггл чекбокса браузером, и обработчик клика на
            # обёртке у formstyler — если тот тоже переключает состояние
            # безусловно, оба переключения гасят друг друга (подтверждено
            # тестом). Программный `el.click()` через JS всегда бьёт точно
            # по обёртке независимо от геометрии/наложения, без гонки.
            await wrapper.evaluate("el => el.click()")
            if await checkbox.is_checked():
                return True
        except PWTimeoutError:
            pass

    # Фолбэк на случай, если formstyler для этого чекбокса не
    # инициализировался и обёртки нет (обычный необёрнутый <input>).
    try:
        await checkbox.evaluate(
            "el => { el.checked = true; "
            "el.dispatchEvent(new Event('click', { bubbles: true })); "
            "el.dispatchEvent(new Event('change', { bubbles: true })); "
            "el.dispatchEvent(new Event('input', { bubbles: true })); }"
        )
    except PWTimeoutError:
        return False

    try:
        return await checkbox.is_checked()
    except PWTimeoutError:
        return False


async def dismiss_unexpected_modals(page: Page) -> bool:
    """Закрывает любые неожиданно показанные Bootstrap-модалки (обрыв
    соединения с backend, устаревшие результаты поиска и т.п.), кроме тех,
    что мы держим открытыми намеренно (форма входа).

    На сайте как минимум два разных всплывающих окна такого рода —
    "Результаты поиска могли устареть" (#timeout-popup) и "Соединение с
    системой продажи проездных документов было прекращено из-за длительного
    простоя" (точный id не установлен — появляется на странице выбора мест
    и не найден статическим анализом JS). Вместо того чтобы перечислять их
    по одному, закрываем любую видимую модалку Bootstrap 3 (класс "in"),
    кроме находящейся в ALLOWED_OPEN_MODAL_IDS.
    """
    dismissed_any = False
    modals = page.locator(".modal.in, .modal.show")
    count = await modals.count()
    for i in range(count):
        modal = modals.nth(i)
        try:
            modal_id = await modal.get_attribute("id") or ""
            if modal_id in ALLOWED_OPEN_MODAL_IDS:
                continue
            if not await modal.is_visible():
                continue
            close_btn = modal.locator(GENERIC_MODAL_CLOSE_SELECTOR).first
            if await close_btn.count() > 0:
                await close_btn.click(timeout=3000)
            else:
                await page.keyboard.press("Escape")
            dismissed_any = True
        except PWTimeoutError:
            continue
    return dismissed_any


async def dismiss_known_overlays(page: Page) -> None:
    """Закрывает все известные блокирующие оверлеи (cookie-баннер и любые
    неожиданные модалки). Вызывайте перед каждым важным кликом.
    """
    await dismiss_cookie_banner(page)
    await dismiss_unexpected_modals(page)


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
    await dismiss_known_overlays(page)

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


async def _extract_train_number(row: Locator, text: str) -> Optional[str]:
    """Извлекает номер поезда из ссылки строки (?train=876Щ&...), если она
    есть — это надёжнее регулярки по тексту, которая путается на составных
    маршрутах ("6918/6510 ... (согласованные поезда)", параметр thread=)
    и на 4-значных номерах пригородных поездов. При отсутствии подходящей
    ссылки используется регулярка по тексту как запасной вариант.
    """
    link = row.locator('a[href*="train="]').first
    if await link.count() > 0:
        href = await link.get_attribute("href")
        if href:
            query = parse_qs(urlparse(href).query)
            values = query.get("train")
            if values:
                return unquote(values[0])

    train_number_match = re.search(r"\b\d{2,3}[А-ЯЁ]\b", text)
    return train_number_match.group(0) if train_number_match else None


async def _collect_train_cards(page: Page) -> list[_TrainCard]:
    """Собирает карточки поездов (строки таблицы результатов поиска).

    Каждая строка — div.sch-table__row-wrap. Номер поезда берётся из
    параметра train= в ссылке строки (надёжнее регулярки по тексту), время
    отправления — из текста строки, а наличие мест определяется по классу
    "w_places".
    """
    rows = page.locator(TRAIN_ROW_SELECTOR)
    count = await rows.count()
    cards: list[_TrainCard] = []
    for i in range(count):
        loc = rows.nth(i)
        text = (await loc.inner_text()).strip()
        classes = (await loc.get_attribute("class")) or ""
        time_match = re.search(r"\b([01]\d|2[0-3]):[0-5]\d\b", text)
        cards.append(
            _TrainCard(
                locator=loc,
                index=i,
                train_number=await _extract_train_number(loc, text),
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
    await dismiss_known_overlays(page)

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

    ВАЖНО: здесь намеренно не используется `wait_for_load_state("networkidle")`.
    На этой странице, судя по всему, держится фоновое соединение с backend
    продажи билетов (вероятно то самое, чей обрыв показывает модалку про
    "длительный простой") — из-за него networkidle может не наступать по
    10-15 секунд на каждом шаге просто потому, что сеть никогда не "затихает",
    а не потому, что страница ещё не готова. Многократное суммирование таких
    ожиданий по всей цепочке шагов, похоже, и даёт достаточную паузу, чтобы
    backend решил, что соединение простаивает. Вместо этого ждём появления
    конкретного нужного элемента — это быстрее и точнее отражает готовность
    страницы.
    """
    try:
        await page.wait_for_selector(CAR_TYPE_ITEM_SELECTOR, timeout=10000)
    except PWTimeoutError:
        logger.warning("Не найден список типов вагонов на странице выбора мест")
        return False
    await dismiss_known_overlays(page)

    type_link = None
    if car_type:
        candidate = page.locator(CAR_TYPE_ITEM_SELECTOR).filter(has_text=car_type).first
        if await candidate.count() > 0:
            type_link = candidate
    if type_link is None:
        type_link = page.locator(CAR_TYPE_ITEM_SELECTOR).first

    # Если у поезда всего один тип вагона, он уже выбран по умолчанию
    # (класс "active") — лишний клик по нему не нужен и может даже
    # провоцировать зависание из-за перекрытия липкой панелью снизу
    # (см. _click_robust).
    type_classes = (await type_link.get_attribute("class")) or ""
    if "active" not in type_classes.split():
        if not await _click_robust(type_link, timeout=8000):
            logger.warning("Не удалось нажать на тип вагона (перекрыт другим элементом)")
            return False

    try:
        await page.wait_for_selector(CARRIAGE_ACCORDION_HEADER_SELECTOR, timeout=10000)
    except PWTimeoutError:
        logger.warning("Не найден список вагонов после выбора типа вагона")
        return False
    await dismiss_known_overlays(page)

    carriage_header = page.locator(CARRIAGE_ACCORDION_HEADER_SELECTOR).first
    if not await _click_robust(carriage_header, timeout=8000):
        logger.warning("Не удалось раскрыть список вагона (перекрыт другим элементом)")
        return False
    # Раскрытие аккордеона — это CSS-анимация (~350 мс у Bootstrap collapse),
    # а не переход страницы, поэтому ждём именно её завершения, а не сети.
    try:
        await page.wait_for_selector(
            CARRIAGE_ACCORDION_EXPANDED_SELECTOR, timeout=5000
        )
    except PWTimeoutError:
        # Не критично: сайт может назначать место автоматически без явного
        # раскрытия — просто даём анимации время устояться.
        await page.wait_for_timeout(600)
    await dismiss_known_overlays(page)
    return True


async def book_train(page: Page, match: TrainMatch, passenger: Passenger, car_type: Optional[str] = None) -> bool:
    """Пытается забронировать найденный поезд для указанного пассажира.

    Возвращает True, если бронирование дошло до подтверждения (карточка
    заказа/бронь создана). Если билеты закончились прямо во время попытки
    (гонка с другими покупателями), возвращает False, чтобы поллер
    продолжил проверку дальше.
    """
    await dismiss_known_overlays(page)
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

    # Дальше не ждём networkidle: следующий шаг сам дожидается появления
    # нужного элемента (см. комментарий в _select_car_type_and_carriage).
    await dismiss_known_overlays(page)

    if not await _select_car_type_and_carriage(page, car_type):
        await _dump_debug_state(page, "car_type_carriage")
        raise SiteInteractionError(
            "Не удалось выбрать тип вагона/вагон на странице /ru/order/places/. "
            "Проверьте CAR_TYPE_ITEM_SELECTOR / CARRIAGE_ACCORDION_HEADER_SELECTOR "
            "в rw_site.py на актуальной разметке."
        )

    await dismiss_known_overlays(page)
    entered = await _click_first_match(page, ENTER_PASSENGERS_TEXT_CANDIDATES, timeout=10000)
    if not entered:
        logger.warning("Не удалось найти кнопку 'Ввести данные пассажиров'")
        return False

    # Ждём один из двух исходов вместо фиксированного networkidle: либо
    # переход на страницу данных пассажиров, либо повторный показ формы
    # входа (истёкшая сессия). Аналогично login().
    auth_modal = page.locator(AUTH_MODAL_SELECTOR)
    deadline_steps = 20  # 20 * 0.5s = 10s максимум
    for _ in range(deadline_steps):
        if PASSENGERS_URL_FRAGMENT in page.url:
            break
        if await auth_modal.count() > 0 and await auth_modal.is_visible():
            await _dump_debug_state(page, "session_expired")
            raise SiteInteractionError(
                "Сайт снова показал форму входа при переходе к данным пассажиров — "
                "сессия авторизации, похоже, истекла или недействительна."
            )
        await page.wait_for_timeout(500)

    if PASSENGERS_URL_FRAGMENT not in page.url:
        logger.warning(
            "После 'Ввести данные пассажиров' не произошёл переход на %s (url=%s)",
            PASSENGERS_URL_FRAGMENT,
            page.url,
        )
        return False

    filled_last = await _fill_first_match(
        page, passenger.last_name, LAST_NAME_LABEL_CANDIDATES, LAST_NAME_NAME_CANDIDATES
    )
    filled_first = await _fill_first_match(
        page, passenger.first_name, FIRST_NAME_LABEL_CANDIDATES, FIRST_NAME_NAME_CANDIDATES
    )
    if passenger.middle_name:
        await _fill_first_match(
            page, passenger.middle_name, MIDDLE_NAME_LABEL_CANDIDATES, MIDDLE_NAME_NAME_CANDIDATES
        )
    filled_doc_number = await _fill_first_match(
        page, passenger.document_number, DOCUMENT_NUMBER_LABEL_CANDIDATES, DOCUMENT_NUMBER_NAME_CANDIDATES
    )
    selected_doc_type = await _select_document_type(page, passenger.document_type)
    agreed = await _check_agreement_checkbox(page)

    if not all([filled_last, filled_first, filled_doc_number, selected_doc_type, agreed]):
        await _dump_debug_state(page, "passenger_form")
        raise SiteInteractionError(
            "Не удалось полностью заполнить форму пассажира "
            f"(фамилия={filled_last}, имя={filled_first}, номер документа={filled_doc_number}, "
            f"тип документа={selected_doc_type}, согласие с правилами={agreed}). "
            "Откалибруйте соответствующие константы в rw_site.py по реальной разметке формы."
        )

    # Кнопка "Оформить заказ" по умолчанию disabled и включается JS-валидацией
    # только после того, как все обязательные поля прошли проверку — ждём
    # явно, а не полагаемся на то, что клик по disabled-кнопке что-то даст.
    submit_button = page.get_by_text("Оформить заказ", exact=False).first
    try:
        await page.wait_for_function(
            "(btn) => btn && !btn.disabled",
            arg=await submit_button.element_handle(),
            timeout=8000,
        )
    except PWTimeoutError:
        await _dump_debug_state(page, "submit_disabled")
        raise SiteInteractionError(
            "Кнопка 'Оформить заказ' осталась disabled после заполнения формы — "
            "вероятно, какое-то обязательное поле не прошло JS-валидацию "
            "(например, формат номера документа) или на форме появилось поле, "
            "которое сейчас не заполняется (дата рождения/пол/гражданство для "
            "некоторых типов документов)."
        )

    confirmed = await _click_robust(submit_button, timeout=8000)
    if not confirmed:
        await _dump_debug_state(page, "submit_click")
        raise SiteInteractionError("Не удалось нажать кнопку 'Оформить заказ'")

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
