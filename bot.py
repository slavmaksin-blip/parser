"""Telegram bot for monitoring Ricardo.ch listings (aiogram 3.x)."""

import asyncio
import os
import random
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from loguru import logger

import db
from scraper import CATEGORIES, Listing, enrich_seller_info, probe_or_search

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

router = Router()

# ─── Constants ────────────────────────────────────────────────────────────────
MAX_LISTINGS_PER_CYCLE = 5   # max new listings sent per monitoring cycle

# ─── FSM States ───────────────────────────────────────────────────────────────

class FilterStates(StatesGroup):
    ST_MENU = State()
    ST_KEYWORDS = State()
    ST_CATEGORY = State()
    ST_MIN_PRICE = State()
    ST_MAX_PRICE = State()
    ST_SELLER_DATE = State()
    ST_MIN_SOLD = State()
    ST_MAX_SOLD = State()
    ST_MIN_PURCHASES = State()
    ST_MAX_PURCHASES = State()
    ST_LISTING_DATE_FROM = State()
    ST_LISTING_DATE_TO = State()
    ST_LISTING_TYPE = State()
    ST_CONDITION = State()
    ST_LOCATION = State()
    ST_DELIVERY = State()


# ─── Per-user search tasks ────────────────────────────────────────────────────
_search_tasks: dict[int, asyncio.Task] = {}

# ─── Button labels ────────────────────────────────────────────────────────────
BTN_START  = "🔍 Начать поиск"
BTN_STOP   = "⏹ Остановить поиск"
BTN_FILTER = "⚙️ Фильтры"
BTN_STATUS = "📊 Статус"


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_STOP)],
            [KeyboardButton(text=BTN_FILTER), KeyboardButton(text=BTN_STATUS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _filter_menu_keyboard(buf: dict) -> InlineKeyboardMarkup:
    kws = buf.get("keywords") or []
    cats = buf.get("categories") or []
    min_p = buf.get("min_price")
    max_p = buf.get("max_price")
    seller_date = buf.get("max_seller_reg_date")
    min_sold = buf.get("min_sold")
    max_sold = buf.get("max_sold")
    min_purchases = buf.get("min_purchases")
    max_purchases = buf.get("max_purchases")
    listing_date_from = buf.get("listing_date_from")
    listing_date_to = buf.get("listing_date_to")
    listing_type = buf.get("listing_type")
    condition = buf.get("condition")
    location = buf.get("location")
    delivery = buf.get("delivery")

    kw_str = ", ".join(kws) if kws else "—"
    cat_str = ", ".join(cats) if cats else "—"

    rows = [
        [InlineKeyboardButton(
            text=f"🔤 Ключевые слова ({kw_str[:30]})",
            callback_data="edit_keywords")],
        [InlineKeyboardButton(
            text=f"📂 Категории ({cat_str[:30]})",
            callback_data="edit_category")],
        [InlineKeyboardButton(
            text=f"💰 Цена от ({min_p if min_p is not None else '—'} CHF)",
            callback_data="edit_min_price")],
        [InlineKeyboardButton(
            text=f"💰 Цена до ({max_p if max_p is not None else '—'} CHF)",
            callback_data="edit_max_price")],
        [InlineKeyboardButton(
            text=f"📅 Дата публ. от ({listing_date_from or '—'})",
            callback_data="edit_listing_date_from")],
        [InlineKeyboardButton(
            text=f"📅 Дата публ. до ({listing_date_to or '—'})",
            callback_data="edit_listing_date_to")],
        [InlineKeyboardButton(
            text=f"🗓 Продавец зарег. до ({seller_date or '—'})",
            callback_data="edit_seller_date")],
        [InlineKeyboardButton(
            text=f"📦 Продаж мин. ({min_sold if min_sold is not None else '—'})",
            callback_data="edit_min_sold"),
         InlineKeyboardButton(
            text=f"📦 Продаж макс. ({max_sold if max_sold is not None else '—'})",
            callback_data="edit_max_sold")],
        [InlineKeyboardButton(
            text=f"🛒 Покупок мин. ({min_purchases if min_purchases is not None else '—'})",
            callback_data="edit_min_purchases"),
         InlineKeyboardButton(
            text=f"🛒 Покупок макс. ({max_purchases if max_purchases is not None else '—'})",
            callback_data="edit_max_purchases")],
        [InlineKeyboardButton(
            text=f"🏷 Тип лота ({listing_type or 'Все'})",
            callback_data="edit_listing_type")],
        [InlineKeyboardButton(
            text=f"🔧 Состояние ({condition or 'Все'})",
            callback_data="edit_condition")],
        [InlineKeyboardButton(
            text=f"📍 Локация ({location or '—'})",
            callback_data="edit_location")],
        [InlineKeyboardButton(
            text=f"🚚 Доставка ({delivery or 'Все'})",
            callback_data="edit_delivery")],
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="save_filters"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_filters"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(CATEGORIES.items())
    for i in range(0, len(items), 2):
        row = []
        for key, label in items[i:i+2]:
            row.append(InlineKeyboardButton(text=label, callback_data=f"cat_{key}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🌐 Все категории", callback_data="cat_all")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_filters")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _listing_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Sofortkauf", callback_data="lt_Sofortkauf"),
            InlineKeyboardButton(text="Auktion",    callback_data="lt_Auktion"),
        ],
        [
            InlineKeyboardButton(text="Festpreis",  callback_data="lt_Festpreis"),
            InlineKeyboardButton(text="Все",         callback_data="lt_all"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_filters")],
    ])


def _condition_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Neu",      callback_data="cond_Neu"),
            InlineKeyboardButton(text="Gebraucht", callback_data="cond_Gebraucht"),
        ],
        [
            InlineKeyboardButton(text="Wie neu",  callback_data="cond_Wie neu"),
            InlineKeyboardButton(text="Defekt",   callback_data="cond_Defekt"),
        ],
        [InlineKeyboardButton(text="Alle",        callback_data="cond_all")],
        [InlineKeyboardButton(text="◀️ Назад",    callback_data="back_to_filters")],
    ])


def _delivery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Versand",  callback_data="del_Versand"),
            InlineKeyboardButton(text="Abholung", callback_data="del_Abholung"),
        ],
        [InlineKeyboardButton(text="Beides",      callback_data="del_Beides")],
        [InlineKeyboardButton(text="◀️ Назад",    callback_data="back_to_filters")],
    ])


def _filter_summary(f: dict) -> str:
    kws = f.get("keywords") or []
    cats = f.get("categories") or []
    lines = [
        f"🔤 <b>Ключевые слова:</b> {', '.join(kws) if kws else '—'}",
        f"📂 <b>Категории:</b> {', '.join(cats) if cats else '—'}",
        f"💰 <b>Цена от:</b> {f['min_price']} CHF" if f.get("min_price") else "💰 <b>Цена от:</b> —",
        f"💰 <b>Цена до:</b> {f['max_price']} CHF" if f.get("max_price") else "💰 <b>Цена до:</b> —",
        f"📅 <b>Дата публ. от:</b> {f['listing_date_from']}"
            if f.get("listing_date_from") else "📅 <b>Дата публ. от:</b> —",
        f"📅 <b>Дата публ. до:</b> {f['listing_date_to']}"
            if f.get("listing_date_to") else "📅 <b>Дата публ. до:</b> —",
        f"🗓 <b>Продавец зарег. до:</b> {f['max_seller_reg_date']}"
            if f.get("max_seller_reg_date") else "🗓 <b>Продавец зарег. до:</b> —",
        f"📦 <b>Продаж от:</b> {f['min_sold']}" if f.get("min_sold") else "📦 <b>Продаж от:</b> —",
        f"📦 <b>Продаж до:</b> {f['max_sold']}" if f.get("max_sold") else "📦 <b>Продаж до:</b> —",
        f"🛒 <b>Покупок от:</b> {f['min_purchases']}" if f.get("min_purchases") else "🛒 <b>Покупок от:</b> —",
        f"🛒 <b>Покупок до:</b> {f['max_purchases']}" if f.get("max_purchases") else "🛒 <b>Покупок до:</b> —",
        f"🏷 <b>Тип лота:</b> {f['listing_type']}" if f.get("listing_type") else "🏷 <b>Тип лота:</b> Все",
        f"🔧 <b>Состояние:</b> {f['condition']}" if f.get("condition") else "🔧 <b>Состояние:</b> Все",
        f"📍 <b>Локация:</b> {f['location']}" if f.get("location") else "📍 <b>Локация:</b> —",
        f"🚚 <b>Доставка:</b> {f['delivery']}" if f.get("delivery") else "🚚 <b>Доставка:</b> Все",
    ]
    return "\n".join(lines)


def _listing_keyboard(listing_id: str, listing_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Открыть на Ricardo", url=listing_url),
        InlineKeyboardButton(text="🙈 Скрыть", callback_data=f"hide_{listing_id}"),
    ]])


# ─── Search loop ──────────────────────────────────────────────────────────────

async def _search_loop(user_id: int, chat_id: int, bot: Bot) -> None:
    logger.info("Search loop started for user {}", user_id)
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            while True:
                if not await db.is_active(user_id):
                    break

                try:
                    f = await db.get_filters(user_id)
                    listings = await probe_or_search(session, f, n=50)
                    await enrich_seller_info(session, listings)

                    sent = 0
                    for listing in listings:
                        if not await db.is_active(user_id):
                            return
                        if sent >= MAX_LISTINGS_PER_CYCLE:
                            break
                        if await db.is_seen(user_id, listing.listing_id):
                            continue
                        if not listing.matches(f):
                            continue
                        await db.mark_seen(user_id, listing.listing_id)
                        try:
                            msg = listing.format_message()
                            kb = _listing_keyboard(listing.listing_id, listing.url)
                            if listing.image_url:
                                await bot.send_photo(
                                    chat_id=chat_id,
                                    photo=listing.image_url,
                                    caption=msg,
                                    parse_mode="HTML",
                                    reply_markup=kb,
                                )
                            else:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=msg,
                                    parse_mode="HTML",
                                    reply_markup=kb,
                                    disable_web_page_preview=False,
                                )
                            sent += 1
                        except Exception as send_exc:
                            logger.error(
                                "Error sending listing {} to user {}: {}",
                                listing.listing_id, user_id, send_exc,
                            )

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Probe error for user {}: {}", user_id, exc)
                    await asyncio.sleep(300)
                    continue

                sleep_time = random.randint(30, 90)
                logger.info("User {} — спим {} сек", user_id, sleep_time)
                await asyncio.sleep(sleep_time)

    except asyncio.CancelledError:
        logger.info("Search loop cancelled for user {}", user_id)
    finally:
        _search_tasks.pop(user_id, None)
        logger.info("Search loop ended for user {}", user_id)


def _start_search_task(user_id: int, chat_id: int, bot: Bot) -> None:
    existing = _search_tasks.pop(user_id, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_search_loop(user_id, chat_id, bot))
    _search_tasks[user_id] = task


# ─── Handlers ─────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    await db.ensure_user(user_id)
    if await db.is_active(user_id) and user_id not in _search_tasks:
        _start_search_task(user_id, message.chat.id, bot)
    await message.answer(
        "👋 <b>Монитор объявлений Ricardo.ch</b>\n\n"
        "Нажми <b>🔍 Начать поиск</b> — бот будет искать объявления и присылать "
        "те, что подходят под твои фильтры.\n\n"
        "Настрой фильтры через <b>⚙️ Фильтры</b> перед запуском.",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    user_id = message.from_user.id
    await db.set_active(user_id, False)
    task = _search_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    await message.answer(
        "⏹ <b>Поиск остановлен.</b>",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@router.message(Command("status"))
@router.message(F.text == BTN_STATUS)
async def cmd_status(message: Message) -> None:
    user_id = message.from_user.id
    await db.ensure_user(user_id)
    active = await db.is_active(user_id)
    f = await db.get_filters(user_id)
    status_str = "✅ Активен" if active else "⛔ Остановлен"
    text = (
        "📊 <b>Статус мониторинга</b>\n\n"
        f"🔄 Поиск: {status_str}\n"
        "⏱ Интервал: 30–90 сек\n\n"
        "<b>Текущие фильтры:</b>\n"
        f"{_filter_summary(f)}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_main_keyboard())


@router.message(F.text == BTN_START)
async def btn_start_search(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    await db.ensure_user(user_id)
    await db.set_active(user_id, True)
    _start_search_task(user_id, message.chat.id, bot)
    await message.answer(
        "🔍 <b>Поиск запущен!</b>\n\n"
        "Ищу объявления на Ricardo.ch и буду присылать те, "
        "что подходят под фильтры. Нажми <b>⏹ Остановить поиск</b> чтобы остановить.",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@router.message(F.text == BTN_STOP)
async def btn_stop_search(message: Message) -> None:
    user_id = message.from_user.id
    await db.set_active(user_id, False)
    task = _search_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    await message.answer(
        "⏹ <b>Поиск остановлен.</b>",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# ─── Filter menu ──────────────────────────────────────────────────────────────

async def _show_filter_menu(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buf = data.get("buf", {})
    await state.set_state(FilterStates.ST_MENU)
    await message.answer(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


@router.message(Command("filters"))
@router.message(Command("add"))
@router.message(F.text == BTN_FILTER)
async def cmd_filters(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    await db.ensure_user(user_id)
    current = await db.get_filters(user_id)
    await state.update_data(buf=dict(current))
    await _show_filter_menu(message, state)


# ─── Callback: filter inline menu ─────────────────────────────────────────────

@router.callback_query(F.data == "back_to_filters")
async def cb_back_to_filters(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    data = await state.get_data()
    buf = data.get("buf", {})
    await state.set_state(FilterStates.ST_MENU)
    await call.message.edit_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


@router.callback_query(F.data == "edit_keywords")
async def cb_edit_keywords(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_KEYWORDS)
    await call.message.answer(
        "🔤 <b>Ключевые слова</b>\n\n"
        "Введи слова через запятую (напр. <code>iPhone, MacBook</code>).\n"
        "Или отправь <code>-</code> для очистки:",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_category")
async def cb_edit_category(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_CATEGORY)
    await call.message.edit_text(
        "📂 <b>Выбери категорию:</b>",
        parse_mode="HTML",
        reply_markup=_category_keyboard(),
    )


@router.callback_query(F.data == "edit_min_price")
async def cb_edit_min_price(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MIN_PRICE)
    await call.message.answer(
        "💰 <b>Минимальная цена</b>\n\nВведи цену в CHF (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_max_price")
async def cb_edit_max_price(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MAX_PRICE)
    await call.message.answer(
        "💰 <b>Максимальная цена</b>\n\nВведи цену в CHF (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_seller_date")
async def cb_edit_seller_date(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_SELLER_DATE)
    await call.message.answer(
        "📅 <b>Дата регистрации продавца</b>\n\n"
        "Показывать продавцов, зарегистрированных <b>до</b> этой даты.\n"
        "Формат: <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ</code>\n"
        "Или <code>-</code> для отключения:",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_min_sold")
async def cb_edit_min_sold(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MIN_SOLD)
    await call.message.answer(
        "📦 <b>Минимум продаж у продавца</b>\n\nВведи число (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_max_sold")
async def cb_edit_max_sold(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MAX_SOLD)
    await call.message.answer(
        "📦 <b>Максимум продаж у продавца</b>\n\nВведи число (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_min_purchases")
async def cb_edit_min_purchases(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MIN_PURCHASES)
    await call.message.answer(
        "🛒 <b>Минимум покупок у продавца</b>\n\nВведи число (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_max_purchases")
async def cb_edit_max_purchases(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_MAX_PURCHASES)
    await call.message.answer(
        "🛒 <b>Максимум покупок у продавца</b>\n\nВведи число (или 0 для отключения):",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_listing_date_from")
async def cb_edit_listing_date_from(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_LISTING_DATE_FROM)
    await call.message.answer(
        "📅 <b>Дата публикации — от</b>\n\n"
        "Показывать объявления, опубликованные <b>после</b> этой даты.\n"
        "Формат: <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n"
        "Или <code>-</code> для отключения:",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_listing_date_to")
async def cb_edit_listing_date_to(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_LISTING_DATE_TO)
    await call.message.answer(
        "📅 <b>Дата публикации — до</b>\n\n"
        "Показывать объявления, опубликованные <b>до</b> этой даты.\n"
        "Формат: <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>\n"
        "Или <code>-</code> для отключения:",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_listing_type")
async def cb_edit_listing_type(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_LISTING_TYPE)
    await call.message.edit_text(
        "🏷 <b>Тип лота:</b>",
        parse_mode="HTML",
        reply_markup=_listing_type_keyboard(),
    )


@router.callback_query(F.data == "edit_condition")
async def cb_edit_condition(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_CONDITION)
    await call.message.edit_text(
        "🔧 <b>Состояние товара:</b>",
        parse_mode="HTML",
        reply_markup=_condition_keyboard(),
    )


@router.callback_query(F.data == "edit_location")
async def cb_edit_location(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_LOCATION)
    await call.message.answer(
        "📍 <b>Локация</b>\n\n"
        "Введи город или регион (напр. <code>Zürich</code>).\n"
        "Или <code>-</code> для отключения:",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "edit_delivery")
async def cb_edit_delivery(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterStates.ST_DELIVERY)
    await call.message.edit_text(
        "🚚 <b>Способ доставки:</b>",
        parse_mode="HTML",
        reply_markup=_delivery_keyboard(),
    )


@router.callback_query(F.data == "save_filters")
async def cb_save_filters(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    user_id = call.from_user.id
    data = await state.get_data()
    buf = data.get("buf", {})
    await db.save_filters(user_id, buf)
    await state.clear()
    await call.message.edit_text(
        "✅ <b>Фильтры сохранены!</b>",
        parse_mode="HTML",
    )
    await call.message.answer(
        f"Текущие фильтры:\n{_filter_summary(buf)}",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@router.callback_query(F.data == "cancel_filters")
async def cb_cancel_filters(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.edit_text("❌ <b>Изменения отменены.</b>", parse_mode="HTML")


# ─── Category selection callbacks ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("cat_"), FilterStates.ST_CATEGORY)
async def cb_select_category(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    key = call.data[4:]  # strip "cat_"
    data = await state.get_data()
    buf = data.get("buf", {})
    if key == "all":
        buf["categories"] = []
    else:
        cats = list(buf.get("categories") or [])
        label = CATEGORIES.get(key, key)
        if label in cats:
            cats.remove(label)
        else:
            cats.append(label)
        buf["categories"] = cats
    await state.update_data(buf=buf)
    await state.set_state(FilterStates.ST_MENU)
    await call.message.edit_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


# ─── Listing type callbacks ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("lt_"), FilterStates.ST_LISTING_TYPE)
async def cb_select_listing_type(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    value = call.data[3:]  # strip "lt_"
    data = await state.get_data()
    buf = data.get("buf", {})
    buf["listing_type"] = None if value == "all" else value
    await state.update_data(buf=buf)
    await state.set_state(FilterStates.ST_MENU)
    await call.message.edit_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


# ─── Condition callbacks ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cond_"), FilterStates.ST_CONDITION)
async def cb_select_condition(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    raw = call.data[5:]  # strip "cond_"
    data = await state.get_data()
    buf = data.get("buf", {})
    buf["condition"] = None if raw == "all" else raw
    await state.update_data(buf=buf)
    await state.set_state(FilterStates.ST_MENU)
    await call.message.edit_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


# ─── Delivery callbacks ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("del_"), FilterStates.ST_DELIVERY)
async def cb_select_delivery(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    raw = call.data[4:]  # strip "del_"
    data = await state.get_data()
    buf = data.get("buf", {})
    buf["delivery"] = None if raw == "Beides" else raw
    await state.update_data(buf=buf)
    await state.set_state(FilterStates.ST_MENU)
    await call.message.edit_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode="HTML",
        reply_markup=_filter_menu_keyboard(buf),
    )


# ─── Text input FSM handlers ───────────────────────────────────────────────────

@router.message(FilterStates.ST_KEYWORDS)
async def fsm_keywords(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    if text == "-":
        buf["keywords"] = []
    else:
        buf["keywords"] = [k.strip() for k in text.split(",") if k.strip()]
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MIN_PRICE)
async def fsm_min_price(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = float(text.replace(",", "."))
        buf["min_price"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи число, например <code>50</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MAX_PRICE)
async def fsm_max_price(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = float(text.replace(",", "."))
        buf["max_price"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи число, например <code>500</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_SELLER_DATE)
async def fsm_seller_date(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    if text in ("-", "0", ""):
        buf["max_seller_reg_date"] = None
    else:
        # Accept YYYY or YYYY-MM-DD
        import re as _re
        if _re.match(r"^\d{4}$", text):
            text = f"{text}-12-31"
        if not _re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            await message.answer(
                "⚠️ Неверный формат. Используй <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ</code>.",
                parse_mode="HTML",
            )
            return
        buf["max_seller_reg_date"] = text
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MIN_SOLD)
async def fsm_min_sold(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = int(text)
        buf["min_sold"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи целое число, например <code>10</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MAX_SOLD)
async def fsm_max_sold(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = int(text)
        buf["max_sold"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи целое число, например <code>100</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MIN_PURCHASES)
async def fsm_min_purchases(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = int(text)
        buf["min_purchases"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи целое число, например <code>5</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_MAX_PURCHASES)
async def fsm_max_purchases(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    try:
        val = int(text)
        buf["max_purchases"] = None if val <= 0 else val
    except ValueError:
        await message.answer("⚠️ Неверный формат. Введи целое число, например <code>50</code>.",
                             parse_mode="HTML")
        return
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


def _parse_date_input(text: str) -> Optional[str]:
    """Parse a date input (YYYY-MM-DD or YYYY-MM-DD HH:MM) and return ISO string or None."""
    import re as _re
    text = text.strip()
    if text in ("-", "0", ""):
        return None
    # YYYY-MM-DD HH:MM
    if _re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text):
        return text.replace(" ", "T") + ":00"
    # YYYY-MM-DD
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    return None


@router.message(FilterStates.ST_LISTING_DATE_FROM)
async def fsm_listing_date_from(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    parsed = _parse_date_input(text)
    if text not in ("-", "0", "") and parsed is None:
        await message.answer(
            "⚠️ Неверный формат. Используй <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>.",
            parse_mode="HTML",
        )
        return
    buf["listing_date_from"] = parsed
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_LISTING_DATE_TO)
async def fsm_listing_date_to(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    parsed = _parse_date_input(text)
    if text not in ("-", "0", "") and parsed is None:
        await message.answer(
            "⚠️ Неверный формат. Используй <code>ГГГГ-ММ-ДД</code> или <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>.",
            parse_mode="HTML",
        )
        return
    buf["listing_date_to"] = parsed
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


@router.message(FilterStates.ST_LOCATION)
async def fsm_location(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    buf = data.get("buf", {})
    buf["location"] = None if text in ("-", "0", "") else text
    await state.update_data(buf=buf)
    await _show_filter_menu(message, state)


# ─── Hide listing callback ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("hide_"))
async def cb_hide_listing(call: CallbackQuery) -> None:
    await call.answer("Скрыто")
    listing_id = call.data[5:]  # strip "hide_"
    user_id = call.from_user.id
    await db.mark_seen(user_id, listing_id)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_caption(
            caption=(call.message.caption or "") + "\n\n✅ <b>Скрыто</b>",
            parse_mode="HTML",
        )
    except Exception:
        try:
            await call.message.edit_text(
                (call.message.text or "") + "\n\n✅ <b>Скрыто</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    await db.init_db()

    # Restore active users on startup
    active_users = await db.get_active_users()
    logger.info("Восстанавливаем {} активных пользователей", len(active_users))

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.create_task(db.cleanup_old_seen()),
        "interval",
        hours=24,
    )
    scheduler.start()

    logger.info("Бот запущен. Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
