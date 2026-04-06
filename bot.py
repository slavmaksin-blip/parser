"""Telegram bot for monitoring Ricardo.ch new listings."""

import logging
import os
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from scraper import (
    CATEGORIES,
    Listing,
    enrich_seller_info,
    fetch_listings,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ─── Conversation states ──────────────────────────────────────────────────────
(
    ST_MENU,
    ST_CATEGORIES,
    ST_KEYWORDS,
    ST_MIN_PRICE,
    ST_MAX_PRICE,
    ST_SELLER_DATE,
    ST_LISTING_DATE_FROM,
    ST_LISTING_DATE_TO,
    ST_MIN_SOLD,
    ST_MIN_PURCHASES,
) = range(10)

# Temporary in-memory edit buffer per user
_edit_buf: dict[int, dict] = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _filter_summary(f: dict) -> str:
    cats = f.get("categories") or []
    cat_names = [CATEGORIES.get(c, c) for c in cats] if cats else ["Все категории"]
    kws = f.get("keywords") or []
    lines = [
        f"🏷 <b>Категории:</b> {', '.join(cat_names)}",
        f"🔍 <b>Ключевые слова:</b> {', '.join(kws) if kws else '—'}",
        f"💰 <b>Цена от:</b> {f['min_price']} CHF" if f.get("min_price") else "💰 <b>Цена от:</b> —",
        f"💰 <b>Цена до:</b> {f['max_price']} CHF" if f.get("max_price") else "💰 <b>Цена до:</b> —",
        f"📅 <b>Продавец зарегистрирован до:</b> {f['max_seller_reg_date']}" if f.get("max_seller_reg_date") else "📅 <b>Продавец зарегистрирован до:</b> —",
        f"🕐 <b>Публикация товара от:</b> {f['listing_date_from']}" if f.get("listing_date_from") else "🕐 <b>Публикация товара от:</b> —",
        f"🕑 <b>Публикация товара до:</b> {f['listing_date_to']}" if f.get("listing_date_to") else "🕑 <b>Публикация товара до:</b> —",
        f"📦 <b>Минимум продано товаров:</b> {f['min_sold']}" if f.get("min_sold") else "📦 <b>Минимум продано товаров:</b> —",
        f"🛒 <b>Минимум покупок:</b> {f['min_purchases']}" if f.get("min_purchases") else "🛒 <b>Минимум покупок:</b> —",
    ]
    return "\n".join(lines)


def _filter_menu_keyboard(buf: dict) -> InlineKeyboardMarkup:
    cats = buf.get("categories") or []
    cat_names = [CATEGORIES.get(c, c) for c in cats] if cats else ["Все"]
    kws = buf.get("keywords") or []
    min_p = buf.get("min_price")
    max_p = buf.get("max_price")
    seller_date = buf.get("max_seller_reg_date")
    date_from = buf.get("listing_date_from")
    date_to = buf.get("listing_date_to")
    min_sold = buf.get("min_sold")
    min_purchases = buf.get("min_purchases")
    rows = [
        [InlineKeyboardButton(f"🏷 Категории ({', '.join(cat_names)})", callback_data="edit_categories")],
        [InlineKeyboardButton(f"🔍 Ключевые слова ({', '.join(kws) if kws else '—'})", callback_data="edit_keywords")],
        [InlineKeyboardButton(f"💰 Цена от ({min_p if min_p else '—'} CHF)", callback_data="edit_min_price")],
        [InlineKeyboardButton(f"💰 Цена до ({max_p if max_p else '—'} CHF)", callback_data="edit_max_price")],
        [InlineKeyboardButton(f"📅 Продавец до ({seller_date or '—'})", callback_data="edit_seller_date")],
        [InlineKeyboardButton(f"🕐 Публикация от ({date_from or '—'})", callback_data="edit_listing_date_from")],
        [InlineKeyboardButton(f"🕑 Публикация до ({date_to or '—'})", callback_data="edit_listing_date_to")],
        [InlineKeyboardButton(f"📦 Продано мин. ({min_sold if min_sold else '—'})", callback_data="edit_min_sold")],
        [InlineKeyboardButton(f"🛒 Покупок мин. ({min_purchases if min_purchases else '—'})", callback_data="edit_min_purchases")],
        [InlineKeyboardButton("✅ Сохранить и выйти", callback_data="save_filters")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_filters")],
    ]
    return InlineKeyboardMarkup(rows)


def _category_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, label in CATEGORIES.items():
        check = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(f"{check}{label}", callback_data=f"cat_{key}")])
    rows.append([InlineKeyboardButton("🌐 Все категории", callback_data="cat_all_toggle")])
    rows.append([InlineKeyboardButton("⬅️ Назад к меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    await update.message.reply_text(
        "👋 <b>Монитор объявлений Ricardo.ch</b>\n\n"
        "Я уведомляю тебя о новых объявлениях на ricardo.ch по заданным фильтрам.\n\n"
        "<b>Доступные команды:</b>\n"
        "/filter — Настройка фильтров\n"
        "/myfilters — Просмотр текущих фильтров\n"
        "/monitor — Запустить мониторинг\n"
        "/stop — Остановить мониторинг\n"
        "/help — Помощь",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ <b>Помощь</b>\n\n"
        "Бот мониторит Ricardo.ch и уведомляет о новых объявлениях по твоим фильтрам.\n\n"
        "<b>Фильтры:</b>\n"
        "• <b>Категории</b> — В каких категориях искать\n"
        "• <b>Ключевые слова</b> — Поисковые слова (через запятую)\n"
        "• <b>Цена от / до</b> — Диапазон цены в CHF\n"
        "• <b>Продавец зарегистрирован до</b> — Только продавцы, зарег. до этой даты (формат: ГГГГ-ММ-ДД)\n"
        "• <b>Публикация от / до</b> — Диапазон даты публикации товара (формат: ГГГГ-ММ-ДД ЧЧ:ММ)\n"
        "• <b>Продано мин.</b> — Минимальное количество проданных товаров у продавца\n"
        "• <b>Покупок мин.</b> — Минимальное количество покупок у продавца\n\n"
        "/filter — Настройка фильтров\n"
        "/myfilters — Просмотр текущих фильтров\n"
        "/monitor — Запустить мониторинг\n"
        "/stop — Остановить мониторинг",
        parse_mode=ParseMode.HTML,
    )


async def cmd_myfilters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    f = await db.get_filters(user_id)
    active = await db.is_active(user_id)
    status = "🟢 Мониторинг активен" if active else "🔴 Мониторинг остановлен"
    await update.message.reply_text(
        f"{status}\n\n<b>Текущие фильтры:</b>\n{_filter_summary(f)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    await db.set_active(user_id, True)
    job_name = f"check_{user_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        context.job_queue.run_repeating(
            _check_job,
            interval=1800,  # every 30 minutes
            first=10,
            name=job_name,
            data=user_id,
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
    await update.message.reply_text(
        "🟢 Мониторинг запущен! Буду уведомлять о новых объявлениях.\n"
        "Используй /stop чтобы остановить мониторинг.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.set_active(user_id, False)
    job_name = f"check_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    await update.message.reply_text("🔴 Мониторинг остановлен.")


# ─── Filter conversation ───────────────────────────────────────────────────────

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    current = await db.get_filters(user_id)
    _edit_buf[user_id] = dict(current)
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "⚙️ <b>Настройка фильтров</b>\n\nВыбери параметр для изменения:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def filter_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "edit_categories":
        selected = _edit_buf.get(user_id, {}).get("categories") or []
        await query.edit_message_text(
            "🏷 <b>Выбор категорий</b>\n\nНажми на категорию для включения/отключения:",
            parse_mode=ParseMode.HTML,
            reply_markup=_category_keyboard(selected),
        )
        return ST_CATEGORIES

    if data == "edit_keywords":
        await query.edit_message_text(
            "🔍 <b>Ввод ключевых слов</b>\n\n"
            "Введи слова <b>через запятую</b> (или — для отключения):\n"
            "Пример: <code>iPhone, MacBook, Sony</code>",
            parse_mode=ParseMode.HTML,
        )
        return ST_KEYWORDS

    if data == "edit_min_price":
        await query.edit_message_text(
            "💰 <b>Минимальная цена</b>\n\nВведи минимальную цену в CHF (или 0 для отключения):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MIN_PRICE

    if data == "edit_max_price":
        await query.edit_message_text(
            "💰 <b>Максимальная цена</b>\n\nВведи максимальную цену в CHF (или 0 для отключения):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MAX_PRICE

    if data == "edit_seller_date":
        await query.edit_message_text(
            "📅 <b>Дата регистрации продавца</b>\n\n"
            "Показывать только продавцов, зарегистрированных <b>до</b> указанной даты.\n"
            "Формат: <code>ГГГГ-ММ-ДД</code> (например <code>2023-01-01</code>)\n"
            "Или — для отключения:",
            parse_mode=ParseMode.HTML,
        )
        return ST_SELLER_DATE

    if data == "edit_listing_date_from":
        await query.edit_message_text(
            "🕐 <b>Дата публикации товара — ОТ</b>\n\n"
            "Показывать объявления, опубликованные <b>после</b> указанной даты и времени.\n"
            "Формат: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (например <code>2024-01-15 09:00</code>)\n"
            "Или — для отключения:",
            parse_mode=ParseMode.HTML,
        )
        return ST_LISTING_DATE_FROM

    if data == "edit_listing_date_to":
        await query.edit_message_text(
            "🕑 <b>Дата публикации товара — ДО</b>\n\n"
            "Показывать объявления, опубликованные <b>до</b> указанной даты и времени.\n"
            "Формат: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (например <code>2024-12-31 23:59</code>)\n"
            "Или — для отключения:",
            parse_mode=ParseMode.HTML,
        )
        return ST_LISTING_DATE_TO

    if data == "edit_min_sold":
        await query.edit_message_text(
            "📦 <b>Минимум проданных товаров у продавца</b>\n\n"
            "Введи минимальное количество продаж у продавца (или 0 для отключения):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MIN_SOLD

    if data == "edit_min_purchases":
        await query.edit_message_text(
            "🛒 <b>Минимум покупок у продавца</b>\n\n"
            "Введи минимальное количество покупок у продавца (или 0 для отключения):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MIN_PURCHASES

    if data == "save_filters":
        await db.save_filters(user_id, _edit_buf.get(user_id, {}))
        _edit_buf.pop(user_id, None)
        await query.edit_message_text(
            "✅ Фильтры сохранены!\n\nИспользуй /monitor для запуска мониторинга.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if data == "cancel_filters":
        _edit_buf.pop(user_id, None)
        await query.edit_message_text("❌ Редактирование фильтров отменено.")
        return ConversationHandler.END

    return ST_MENU


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "back_to_menu":
        kb = _filter_menu_keyboard(_edit_buf.get(user_id, {}))
        await query.edit_message_text(
            "⚙️ <b>Настройка фильтров</b>\n\nВыбери параметр для изменения:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return ST_MENU

    if data == "cat_all_toggle":
        _edit_buf.setdefault(user_id, {})["categories"] = []
        await query.edit_message_reply_markup(
            reply_markup=_category_keyboard([])
        )
        return ST_CATEGORIES

    if data.startswith("cat_"):
        key = data[4:]
        buf = _edit_buf.setdefault(user_id, {})
        selected: list = list(buf.get("categories") or [])
        if key in selected:
            selected.remove(key)
        else:
            selected.append(key)
        buf["categories"] = selected
        await query.edit_message_reply_markup(
            reply_markup=_category_keyboard(selected)
        )
        return ST_CATEGORIES

    return ST_CATEGORIES


async def keywords_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text in ("—", "-", ""):
        _edit_buf.setdefault(user_id, {})["keywords"] = []
    else:
        kws = [k.strip() for k in text.split(",") if k.strip()]
        _edit_buf.setdefault(user_id, {})["keywords"] = kws
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Ключевые слова сохранены.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def min_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip().replace(",", ".")
    try:
        val = float(text)
        _edit_buf.setdefault(user_id, {})["min_price"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи число.")
        return ST_MIN_PRICE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Минимальная цена сохранена.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def max_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip().replace(",", ".")
    try:
        val = float(text)
        _edit_buf.setdefault(user_id, {})["max_price"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи число.")
        return ST_MAX_PRICE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Максимальная цена сохранена.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def seller_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text in ("—", "-", "0", ""):
        _edit_buf.setdefault(user_id, {})["max_seller_reg_date"] = None
    else:
        try:
            datetime.fromisoformat(text)
            _edit_buf.setdefault(user_id, {})["max_seller_reg_date"] = text
        except ValueError:
            await update.message.reply_text(
                "❌ Неверная дата. Формат: ГГГГ-ММ-ДД (например 2023-01-01)"
            )
            return ST_SELLER_DATE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Дата регистрации продавца сохранена.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


def _parse_datetime_input(text: str) -> str:
    """Accept 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD', raise ValueError if invalid."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            datetime.strptime(text, fmt)
            return text
        except ValueError:
            pass
    raise ValueError(f"Unrecognised datetime: {text}")


async def listing_date_from_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text in ("—", "-", ""):
        _edit_buf.setdefault(user_id, {})["listing_date_from"] = None
    else:
        try:
            _edit_buf.setdefault(user_id, {})["listing_date_from"] = _parse_datetime_input(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Используй: ГГГГ-ММ-ДД ЧЧ:ММ (например 2024-01-15 09:00)"
            )
            return ST_LISTING_DATE_FROM
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Дата публикации (от) сохранена.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def listing_date_to_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text in ("—", "-", ""):
        _edit_buf.setdefault(user_id, {})["listing_date_to"] = None
    else:
        try:
            _edit_buf.setdefault(user_id, {})["listing_date_to"] = _parse_datetime_input(text)
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат. Используй: ГГГГ-ММ-ДД ЧЧ:ММ (например 2024-12-31 23:59)"
            )
            return ST_LISTING_DATE_TO
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Дата публикации (до) сохранена.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def min_sold_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        _edit_buf.setdefault(user_id, {})["min_sold"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи целое число.")
        return ST_MIN_SOLD
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Минимум продаж сохранён.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def min_purchases_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        _edit_buf.setdefault(user_id, {})["min_purchases"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи целое число.")
        return ST_MIN_PURCHASES
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Минимум покупок сохранён.\n\n⚙️ <b>Настройка фильтров:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    _edit_buf.pop(user_id, None)
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ─── Background job ───────────────────────────────────────────────────────────

async def _check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id: int = context.job.data
    if not await db.is_active(user_id):
        return

    f = await db.get_filters(user_id)
    keywords: list = f.get("keywords") or []
    categories: list = f.get("categories") or []

    try:
        async with aiohttp.ClientSession() as session:
            listings = await fetch_listings(session, keywords, categories)

            # Always enrich seller info to get registration date, sold/purchases counts
            await enrich_seller_info(session, listings)

        new_listings: list[Listing] = []
        for listing in listings:
            if await db.is_seen(user_id, listing.listing_id):
                continue
            if not listing.matches(f):
                continue
            new_listings.append(listing)
            await db.mark_seen(user_id, listing.listing_id)

        for listing in new_listings[:10]:  # cap at 10 per run to avoid spam
            try:
                msg = listing.format_message()
                if listing.image_url:
                    await context.bot.send_photo(
                        chat_id=context.job.chat_id,
                        photo=listing.image_url,
                        caption=msg,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=context.job.chat_id,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False,
                    )
            except Exception as exc:
                logger.error("Error sending listing %s: %s", listing.listing_id, exc)

        if not new_listings:
            logger.info("No new listings for user %s", user_id)

    except Exception as exc:
        logger.error("Check job error for user %s: %s", user_id, exc)

    await db.cleanup_old_seen(days=30)


# ─── Bot setup ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Initialize the database after the event loop is running."""
    await db.init_db()


def build_app() -> Application:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("filter", cmd_filter)],
        states={
            ST_MENU: [CallbackQueryHandler(filter_menu_callback)],
            ST_CATEGORIES: [CallbackQueryHandler(category_callback)],
            ST_KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, keywords_input)],
            ST_MIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_price_input)],
            ST_MAX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, max_price_input)],
            ST_SELLER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_date_input)],
            ST_LISTING_DATE_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_date_from_input)],
            ST_LISTING_DATE_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_date_to_input)],
            ST_MIN_SOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_sold_input)],
            ST_MIN_PURCHASES: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_purchases_input)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myfilters", cmd_myfilters))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(conv)

    return app


if __name__ == "__main__":
    app = build_app()
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
