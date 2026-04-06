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
    ST_LISTING_AGE,
) = range(7)

# Temporary in-memory edit buffer per user
_edit_buf: dict[int, dict] = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _filter_summary(f: dict) -> str:
    cats = f.get("categories") or []
    cat_names = [CATEGORIES.get(c, c) for c in cats] if cats else ["Alle"]
    kws = f.get("keywords") or []
    lines = [
        f"🏷 <b>Kategorien:</b> {', '.join(cat_names)}",
        f"🔍 <b>Schlüsselwörter:</b> {', '.join(kws) if kws else '—'}",
        f"💰 <b>Min. Preis:</b> {f['min_price']} CHF" if f.get("min_price") else "💰 <b>Min. Preis:</b> —",
        f"💰 <b>Max. Preis:</b> {f['max_price']} CHF" if f.get("max_price") else "💰 <b>Max. Preis:</b> —",
        f"📅 <b>Max. Registrierungsdatum Verkäufer:</b> {f['max_seller_reg_date']}" if f.get("max_seller_reg_date") else "📅 <b>Max. Reg.-datum Verkäufer:</b> —",
        f"⏰ <b>Max. Alter der Anzeige:</b> {f['max_listing_age_h']} Std." if f.get("max_listing_age_h") else "⏰ <b>Max. Alter der Anzeige:</b> —",
    ]
    return "\n".join(lines)


def _filter_menu_keyboard(buf: dict) -> InlineKeyboardMarkup:
    cats = buf.get("categories") or []
    cat_names = [CATEGORIES.get(c, c) for c in cats] if cats else ["Alle"]
    kws = buf.get("keywords") or []
    min_p = buf.get("min_price")
    max_p = buf.get("max_price")
    seller_date = buf.get("max_seller_reg_date")
    age_h = buf.get("max_listing_age_h")
    rows = [
        [InlineKeyboardButton(f"🏷 Kategorien ({', '.join(cat_names)})", callback_data="edit_categories")],
        [InlineKeyboardButton(f"🔍 Schlüsselwörter ({', '.join(kws) if kws else '—'})", callback_data="edit_keywords")],
        [InlineKeyboardButton(f"💰 Min. Preis ({min_p if min_p else '—'} CHF)", callback_data="edit_min_price")],
        [InlineKeyboardButton(f"💰 Max. Preis ({max_p if max_p else '—'} CHF)", callback_data="edit_max_price")],
        [InlineKeyboardButton(f"📅 Max. Reg.-Datum Verkäufer ({seller_date or '—'})", callback_data="edit_seller_date")],
        [InlineKeyboardButton(f"⏰ Max. Anzeigenalter ({age_h if age_h else '—'} Std.)", callback_data="edit_listing_age")],
        [InlineKeyboardButton("✅ Speichern & Zurück", callback_data="save_filters")],
        [InlineKeyboardButton("❌ Abbrechen", callback_data="cancel_filters")],
    ]
    return InlineKeyboardMarkup(rows)


def _category_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, label in CATEGORIES.items():
        if key == "all":
            continue
        check = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(f"{check}{label}", callback_data=f"cat_{key}")])
    rows.append([
        InlineKeyboardButton("🌐 Alle Kategorien", callback_data="cat_all_toggle"),
    ])
    rows.append([InlineKeyboardButton("⬅️ Zurück zum Menü", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    await update.message.reply_text(
        "👋 <b>Ricardo.ch Listing-Monitor</b>\n\n"
        "Ich benachrichtige dich über neue Inserate auf ricardo.ch, die deinen Filtern entsprechen.\n\n"
        "<b>Verfügbare Befehle:</b>\n"
        "/filter — Filter einstellen\n"
        "/myfilters — Aktuelle Filter anzeigen\n"
        "/monitor — Überwachung starten\n"
        "/stop — Überwachung stoppen\n"
        "/help — Hilfe anzeigen",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ <b>Hilfe</b>\n\n"
        "Dieser Bot überwacht Ricardo.ch auf neue Inserate und benachrichtigt dich nach deinen Filtern.\n\n"
        "<b>Filter:</b>\n"
        "• <b>Kategorien</b> – In welchen Kategorien gesucht wird\n"
        "• <b>Schlüsselwörter</b> – Suchbegriffe (kommagetrennt)\n"
        "• <b>Preisrange</b> – Min. und Max. Preis in CHF\n"
        "• <b>Max. Reg.-Datum Verkäufer</b> – Nur Verkäufer, die sich vor diesem Datum registriert haben (Format: YYYY-MM-DD)\n"
        "• <b>Max. Anzeigenalter</b> – Nur Inserate, die nicht älter als X Stunden sind\n\n"
        "/filter — Filter einstellen\n"
        "/myfilters — Aktuelle Filter anzeigen\n"
        "/monitor — Überwachung starten\n"
        "/stop — Überwachung stoppen",
        parse_mode=ParseMode.HTML,
    )


async def cmd_myfilters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    f = await db.get_filters(user_id)
    active = await db.is_active(user_id)
    status = "🟢 Überwachung aktiv" if active else "🔴 Überwachung inaktiv"
    await update.message.reply_text(
        f"{status}\n\n<b>Aktuelle Filter:</b>\n{_filter_summary(f)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    await db.set_active(user_id, True)
    # Schedule check job if not already running
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
        "🟢 Überwachung gestartet! Du wirst über neue Inserate benachrichtigt.\n"
        "Verwende /stop um die Überwachung zu beenden.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await db.set_active(user_id, False)
    job_name = f"check_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    await update.message.reply_text("🔴 Überwachung gestoppt.")


# ─── Filter conversation ───────────────────────────────────────────────────────

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    await db.ensure_user(user_id)
    current = await db.get_filters(user_id)
    _edit_buf[user_id] = dict(current)
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "⚙️ <b>Filter einstellen</b>\n\nWähle eine Einstellung zum Bearbeiten:",
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
            "🏷 <b>Kategorien auswählen</b>\n\nTippe auf eine Kategorie zum An-/Abwählen:",
            parse_mode=ParseMode.HTML,
            reply_markup=_category_keyboard(selected),
        )
        return ST_CATEGORIES

    if data == "edit_keywords":
        await query.edit_message_text(
            "🔍 <b>Schlüsselwörter eingeben</b>\n\n"
            "Gib die Suchbegriffe <b>kommagetrennt</b> ein (oder — für keine):\n"
            "Beispiel: <code>iPhone, MacBook, Sony</code>",
            parse_mode=ParseMode.HTML,
        )
        return ST_KEYWORDS

    if data == "edit_min_price":
        await query.edit_message_text(
            "💰 <b>Mindestpreis eingeben</b>\n\nGib den Mindestpreis in CHF ein (oder 0 für keinen):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MIN_PRICE

    if data == "edit_max_price":
        await query.edit_message_text(
            "💰 <b>Maximalpreis eingeben</b>\n\nGib den Maximalpreis in CHF ein (oder 0 für keinen):",
            parse_mode=ParseMode.HTML,
        )
        return ST_MAX_PRICE

    if data == "edit_seller_date":
        await query.edit_message_text(
            "📅 <b>Max. Registrierungsdatum des Verkäufers</b>\n\n"
            "Nur Verkäufer anzeigen, die sich <b>vor</b> diesem Datum registriert haben.\n"
            "Format: <code>YYYY-MM-DD</code> (z.B. <code>2023-01-01</code>)\n"
            "Oder — um zu deaktivieren:",
            parse_mode=ParseMode.HTML,
        )
        return ST_SELLER_DATE

    if data == "edit_listing_age":
        await query.edit_message_text(
            "⏰ <b>Max. Alter der Anzeige (Stunden)</b>\n\n"
            "Nur Inserate anzeigen, die nicht älter als X Stunden sind.\n"
            "Gib die Anzahl Stunden ein (z.B. <code>24</code>) oder 0 für keine Begrenzung:",
            parse_mode=ParseMode.HTML,
        )
        return ST_LISTING_AGE

    if data == "save_filters":
        await db.save_filters(user_id, _edit_buf.get(user_id, {}))
        _edit_buf.pop(user_id, None)
        await query.edit_message_text(
            "✅ Filter gespeichert!\n\nVerwende /monitor um die Überwachung zu starten.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if data == "cancel_filters":
        _edit_buf.pop(user_id, None)
        await query.edit_message_text("❌ Filter-Bearbeitung abgebrochen.")
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
            "⚙️ <b>Filter einstellen</b>\n\nWähle eine Einstellung zum Bearbeiten:",
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
    if text == "—" or text == "-" or not text:
        _edit_buf.setdefault(user_id, {})["keywords"] = []
    else:
        kws = [k.strip() for k in text.split(",") if k.strip()]
        _edit_buf.setdefault(user_id, {})["keywords"] = kws
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Schlüsselwörter gespeichert.\n\n⚙️ <b>Filter einstellen</b>:",
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
        await update.message.reply_text("❌ Ungültiger Wert. Bitte eine Zahl eingeben.")
        return ST_MIN_PRICE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Mindestpreis gespeichert.\n\n⚙️ <b>Filter einstellen</b>:",
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
        await update.message.reply_text("❌ Ungültiger Wert. Bitte eine Zahl eingeben.")
        return ST_MAX_PRICE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Maximalpreis gespeichert.\n\n⚙️ <b>Filter einstellen</b>:",
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
                "❌ Ungültiges Datum. Format: YYYY-MM-DD (z.B. 2023-01-01)"
            )
            return ST_SELLER_DATE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Datum gespeichert.\n\n⚙️ <b>Filter einstellen</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def listing_age_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    try:
        val = int(text)
        _edit_buf.setdefault(user_id, {})["max_listing_age_h"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Ungültiger Wert. Bitte eine ganze Zahl eingeben.")
        return ST_LISTING_AGE
    kb = _filter_menu_keyboard(_edit_buf[user_id])
    await update.message.reply_text(
        "✅ Alter gespeichert.\n\n⚙️ <b>Filter einstellen</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return ST_MENU


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    _edit_buf.pop(user_id, None)
    await update.message.reply_text("❌ Abgebrochen.")
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

            # Enrich seller info only when seller date filter is active
            if f.get("max_seller_reg_date"):
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

    # Weekly cleanup of old seen entries
    await db.cleanup_old_seen(days=30)


# ─── Bot setup ────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Initialize the database after the event loop is running."""
    await db.init_db()


def build_app() -> Application:
    app = Application.builder().token(TOKEN).post_init(_post_init).build()

    # Filter conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("filter", cmd_filter)],
        states={
            ST_MENU: [CallbackQueryHandler(filter_menu_callback)],
            ST_CATEGORIES: [CallbackQueryHandler(category_callback)],
            ST_KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, keywords_input)],
            ST_MIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_price_input)],
            ST_MAX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, max_price_input)],
            ST_SELLER_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, seller_date_input)],
            ST_LISTING_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_age_input)],
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
