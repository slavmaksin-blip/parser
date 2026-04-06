"""Telegram bot for monitoring Ricardo.ch new listings."""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
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
    probe_batch,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# ─── Button labels ────────────────────────────────────────────────────────────
BTN_START  = "🔍 Начать поиск"
BTN_STOP   = "⏹ Остановить поиск"
BTN_FILTER = "⚙️ Фильтры"

# ─── Conversation states ──────────────────────────────────────────────────────
(
    ST_MENU,
    ST_MIN_PRICE,
    ST_MAX_PRICE,
    ST_SELLER_DATE,
    ST_LISTING_DATE_FROM,
    ST_LISTING_DATE_TO,
    ST_MIN_SOLD,
    ST_MIN_PURCHASES,
) = range(8)

# ─── Per-user search tasks ────────────────────────────────────────────────────
_search_tasks: dict[int, asyncio.Task] = {}

# Temporary in-memory edit buffer per user
_edit_buf: dict[int, dict] = {}


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_START, BTN_STOP], [BTN_FILTER]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _filter_menu_keyboard(buf: dict) -> InlineKeyboardMarkup:
    min_p = buf.get("min_price")
    max_p = buf.get("max_price")
    seller_date = buf.get("max_seller_reg_date")
    date_from = buf.get("listing_date_from")
    date_to = buf.get("listing_date_to")
    min_sold = buf.get("min_sold")
    min_purchases = buf.get("min_purchases")
    rows = [
        [InlineKeyboardButton(
            f"💰 Цена от ({min_p if min_p else '—'} CHF)", callback_data="edit_min_price")],
        [InlineKeyboardButton(
            f"💰 Цена до ({max_p if max_p else '—'} CHF)", callback_data="edit_max_price")],
        [InlineKeyboardButton(
            f"📅 Продавец зарег. до ({seller_date or '—'})", callback_data="edit_seller_date")],
        [InlineKeyboardButton(
            f"🕐 Публикация от ({date_from or '—'})", callback_data="edit_listing_date_from")],
        [InlineKeyboardButton(
            f"🕑 Публикация до ({date_to or '—'})", callback_data="edit_listing_date_to")],
        [InlineKeyboardButton(
            f"📦 Продано мин. ({min_sold if min_sold else '—'})", callback_data="edit_min_sold")],
        [InlineKeyboardButton(
            f"🛒 Покупок мин. ({min_purchases if min_purchases else '—'})", callback_data="edit_min_purchases")],
        [InlineKeyboardButton("✅ Сохранить", callback_data="save_filters"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel_filters")],
    ]
    return InlineKeyboardMarkup(rows)


def _filter_summary(f: dict) -> str:
    lines = [
        f"💰 <b>Цена от:</b> {f['min_price']} CHF" if f.get("min_price") else "💰 <b>Цена от:</b> —",
        f"💰 <b>Цена до:</b> {f['max_price']} CHF" if f.get("max_price") else "💰 <b>Цена до:</b> —",
        f"📅 <b>Продавец зарег. до:</b> {f['max_seller_reg_date']}"
            if f.get("max_seller_reg_date") else "📅 <b>Продавец зарег. до:</b> —",
        f"🕐 <b>Публикация от:</b> {f['listing_date_from']}"
            if f.get("listing_date_from") else "🕐 <b>Публикация от:</b> —",
        f"🕑 <b>Публикация до:</b> {f['listing_date_to']}"
            if f.get("listing_date_to") else "🕑 <b>Публикация до:</b> —",
        f"📦 <b>Продано мин.:</b> {f['min_sold']}" if f.get("min_sold") else "📦 <b>Продано мин.:</b> —",
        f"🛒 <b>Покупок мин.:</b> {f['min_purchases']}"
            if f.get("min_purchases") else "🛒 <b>Покупок мин.:</b> —",
    ]
    return "\n".join(lines)


# ─── Continuous search loop ───────────────────────────────────────────────────

async def _search_loop(user_id: int, chat_id: int, bot) -> None:
    """
    Continuously probe random Ricardo.ch listing IDs, enrich seller data,
    apply filters and send new matching listings to the user.
    """
    logger.info("Search loop started for user %s", user_id)
    try:
        async with aiohttp.ClientSession() as session:
            while True:
                if not await db.is_active(user_id):
                    break

                try:
                    listings = await probe_batch(session)
                    await enrich_seller_info(session, listings)

                    f = await db.get_filters(user_id)
                    for listing in listings:
                        if not await db.is_active(user_id):
                            return
                        if await db.is_seen(user_id, listing.listing_id):
                            continue
                        if not listing.matches(f):
                            continue
                        await db.mark_seen(user_id, listing.listing_id)
                        try:
                            msg = listing.format_message()
                            if listing.image_url:
                                await bot.send_photo(
                                    chat_id=chat_id,
                                    photo=listing.image_url,
                                    caption=msg,
                                    parse_mode=ParseMode.HTML,
                                )
                            else:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=msg,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=False,
                                )
                        except Exception as send_exc:
                            logger.error(
                                "Error sending listing %s to user %s: %s",
                                listing.listing_id, user_id, send_exc,
                            )

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("Probe error for user %s: %s", user_id, exc)

                # Short pause between batches
                await asyncio.sleep(5)

    except asyncio.CancelledError:
        logger.info("Search loop cancelled for user %s", user_id)
    finally:
        _search_tasks.pop(user_id, None)
        logger.info("Search loop ended for user %s", user_id)


def _start_search_task(user_id: int, chat_id: int, bot) -> None:
    """Cancel any existing search task and start a new one."""
    existing = _search_tasks.pop(user_id, None)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_search_loop(user_id, chat_id, bot))
    _search_tasks[user_id] = task


# ─── Main button handlers ─────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    # If user was already active in DB (e.g. after bot restart), restore task
    if await db.is_active(user_id) and user_id not in _search_tasks:
        _start_search_task(user_id, update.effective_chat.id, context.bot)
    await update.message.reply_text(
        "👋 <b>Монитор объявлений Ricardo.ch</b>\n\n"
        "Нажми <b>🔍 Начать поиск</b> — бот будет перебирать объявления и присылать "
        "те, что подходят под твои фильтры.\n\n"
        "Настрой фильтры через <b>⚙️ Фильтры</b> перед запуском.",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )


async def btn_start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    await db.set_active(user_id, True)
    _start_search_task(user_id, update.effective_chat.id, context.bot)
    await update.message.reply_text(
        "🔍 <b>Поиск запущен!</b>\n\n"
        "Перебираю объявления на Ricardo.ch и буду присылать те, "
        "что подходят под фильтры. Нажми <b>⏹ Остановить поиск</b> чтобы остановить.",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )


async def btn_stop_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.set_active(user_id, False)
    task = _search_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
    await update.message.reply_text(
        "⏹ <b>Поиск остановлен.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )


# ─── Filter conversation ──────────────────────────────────────────────────────

async def btn_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    current = await db.get_filters(user_id)
    _edit_buf[user_id] = dict(current)
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def filter_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    prompts = {
        "edit_min_price": (
            ST_MIN_PRICE,
            "💰 <b>Минимальная цена</b>\n\nВведи цену в CHF (или 0 для отключения):",
        ),
        "edit_max_price": (
            ST_MAX_PRICE,
            "💰 <b>Максимальная цена</b>\n\nВведи цену в CHF (или 0 для отключения):",
        ),
        "edit_seller_date": (
            ST_SELLER_DATE,
            "📅 <b>Дата регистрации продавца</b>\n\n"
            "Показывать продавцов, зарегистрированных <b>до</b> этой даты.\n"
            "Формат: <code>ГГГГ-ММ-ДД</code> или просто <code>ГГГГ</code>\n"
            "Или — для отключения:",
        ),
        "edit_listing_date_from": (
            ST_LISTING_DATE_FROM,
            "🕐 <b>Публикация от</b>\n\n"
            "Формат: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> или <code>ГГГГ-ММ-ДД</code>\n"
            "Или — для отключения:",
        ),
        "edit_listing_date_to": (
            ST_LISTING_DATE_TO,
            "🕑 <b>Публикация до</b>\n\n"
            "Формат: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> или <code>ГГГГ-ММ-ДД</code>\n"
            "Или — для отключения:",
        ),
        "edit_min_sold": (
            ST_MIN_SOLD,
            "📦 <b>Минимум продаж у продавца</b>\n\nВведи число (или 0 для отключения):",
        ),
        "edit_min_purchases": (
            ST_MIN_PURCHASES,
            "🛒 <b>Минимум покупок у продавца</b>\n\nВведи число (или 0 для отключения):",
        ),
    }

    if data in prompts:
        state, text = prompts[data]
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return state

    if data == "save_filters":
        await db.save_filters(user_id, _edit_buf.get(user_id, {}))
        _edit_buf.pop(user_id, None)
        await query.edit_message_text(
            "✅ <b>Фильтры сохранены!</b>",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if data == "cancel_filters":
        _edit_buf.pop(user_id, None)
        await query.edit_message_text("❌ Редактирование отменено.")
        return ConversationHandler.END

    return ST_MENU


def _back_to_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    kb = _filter_menu_keyboard(_edit_buf.get(user_id, {}))
    return "⚙️ <b>Фильтры</b>\n\nНажми на параметр для изменения:", kb


async def min_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip().replace(",", ".")
    try:
        val = float(text)
        _edit_buf.setdefault(user_id, {})["min_price"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи число.")
        return ST_MIN_PRICE
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
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
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ST_MENU


async def seller_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text in ("—", "-", "0", ""):
        _edit_buf.setdefault(user_id, {})["max_seller_reg_date"] = None
    else:
        # Accept YYYY or YYYY-MM-DD
        if re.fullmatch(r"\d{4}", text):
            text = f"{text}-01-01"
        try:
            datetime.fromisoformat(text)
            _edit_buf.setdefault(user_id, {})["max_seller_reg_date"] = text
        except ValueError:
            await update.message.reply_text(
                "❌ Неверная дата. Пример: <code>2023-01-01</code> или <code>2023</code>",
                parse_mode=ParseMode.HTML,
            )
            return ST_SELLER_DATE
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ST_MENU


def _parse_datetime_input(text: str) -> str:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y"):
        try:
            datetime.strptime(text, fmt)
            if fmt == "%Y":
                return f"{text}-01-01"
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
                "❌ Неверный формат. Пример: <code>2024-01-15 09:00</code>",
                parse_mode=ParseMode.HTML,
            )
            return ST_LISTING_DATE_FROM
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
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
                "❌ Неверный формат. Пример: <code>2024-12-31 23:59</code>",
                parse_mode=ParseMode.HTML,
            )
            return ST_LISTING_DATE_TO
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ST_MENU


async def min_sold_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        val = int(update.message.text.strip())
        _edit_buf.setdefault(user_id, {})["min_sold"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи целое число.")
        return ST_MIN_SOLD
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ST_MENU


async def min_purchases_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        val = int(update.message.text.strip())
        _edit_buf.setdefault(user_id, {})["min_purchases"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Неверное значение. Введи целое число.")
        return ST_MIN_PURCHASES
    txt, kb = _back_to_menu(user_id)
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    return ST_MENU


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    _edit_buf.pop(user_id, None)
    if update.message:
        await update.message.reply_text("❌ Отменено.", reply_markup=_main_keyboard())
    return ConversationHandler.END


# ─── Legacy commands ──────────────────────────────────────────────────────────

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow /stop command as alias."""
    await btn_stop_search(update, context)


# ─── Bot setup ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await db.init_db()


def build_app() -> Application:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    # Filter conversation – entry point is the ⚙️ Фильтры button
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(BTN_FILTER)}$"), btn_filters),
            CommandHandler("filter", btn_filters),
        ],
        states={
            ST_MENU: [CallbackQueryHandler(filter_menu_callback)],
            ST_MIN_PRICE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, min_price_input)],
            ST_MAX_PRICE:          [MessageHandler(filters.TEXT & ~filters.COMMAND, max_price_input)],
            ST_SELLER_DATE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_date_input)],
            ST_LISTING_DATE_FROM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_date_from_input)],
            ST_LISTING_DATE_TO:    [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_date_to_input)],
            ST_MIN_SOLD:           [MessageHandler(filters.TEXT & ~filters.COMMAND, min_sold_input)],
            ST_MIN_PURCHASES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, min_purchases_input)],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            MessageHandler(
                filters.Regex(f"^({re.escape(BTN_START)}|{re.escape(BTN_STOP)})$"),
                conv_cancel,
            ),
        ],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(conv)

    # Main button handlers (outside conversation)
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_START)}$"), btn_start_search))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_STOP)}$"), btn_stop_search))

    return app


if __name__ == "__main__":
    app = build_app()
    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
