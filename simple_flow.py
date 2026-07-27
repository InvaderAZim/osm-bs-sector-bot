from __future__ import annotations

import os
import re
from typing import Any

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import app as base

WAIT_METHOD, WAIT_LOCATION, WAIT_ADDRESS, WAIT_CHOICE, WAIT_AZIMUTH = range(10, 15)

BTN_LOCATION = "📍 Надіслати геолокацію"
BTN_ADDRESS = "⌨️ Ввести адресу або координати"
BTN_CANCEL = "❌ Скасувати"
BTN_RESTART = "🔄 Перезапустити бота"
BTN_USERS = "👥 Користувачі"


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    try:
        import user_control
        return user_control.is_admin(user_id)
    except Exception:
        return False


def initial_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_LOCATION, request_location=True)],
        [KeyboardButton(BTN_ADDRESS)],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(BTN_USERS)])
    rows.append([KeyboardButton(BTN_RESTART)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть спосіб визначення точки",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Введіть дані або натисніть «Скасувати»",
    )


async def clear_previous_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    message = update.effective_message
    if message is None:
        return
    try:
        temporary = await message.reply_text("Оновлення меню…", reply_markup=ReplyKeyboardRemove())
        await temporary.delete()
    except Exception:
        pass


async def show_initial_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Оберіть спосіб визначення точки БС:"):
    if await base.deny(update, context):
        return ConversationHandler.END
    await clear_previous_menu(update, context)
    user_id = update.effective_user.id if update.effective_user else None
    await update.effective_message.reply_text(text, reply_markup=initial_keyboard(user_id))
    return WAIT_METHOD


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_initial_menu(update, context)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_initial_menu(update, context, "Бота перезапущено. Оберіть нову дію:")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_initial_menu(update, context, "Поточний запит скасовано.")


async def wait_for_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await base.deny(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Надішліть геолокацію через кнопку нижче. Бот чекатиме саме повідомлення з геолокацією.",
        reply_markup=ReplyKeyboardMarkup(
            [
                [KeyboardButton(BTN_LOCATION, request_location=True)],
                [KeyboardButton(BTN_CANCEL)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Надішліть геолокацію",
        ),
    )
    return WAIT_LOCATION


async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None or message.location is None:
        await message.reply_text("Потрібно надіслати саме геолокацію.", reply_markup=cancel_keyboard())
        return WAIT_LOCATION

    lat = float(message.location.latitude)
    lon = float(message.location.longitude)
    context.user_data["point"] = {"lat": lat, "lon": lon, "label": "Надіслана геолокація"}
    await ask_azimuth(message, lat, lon)
    return WAIT_AZIMUTH


async def ask_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await base.deny(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Введіть адресу в будь-якому зручному форматі або координати.\n\n"
        "Приклади:\n"
        "<code>Житомир Грушевського 5</code>\n"
        "<code>м. Житомир, вул. Грушевського, буд. 5</code>\n"
        "<code>50.2547, 28.6587</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )
    return WAIT_ADDRESS


def normalize_address(text: str) -> str:
    value = re.sub(r"\s+", " ", text.strip())
    replacements = {
        r"\bм\.\s*": "",
        r"\bвул\.\s*": "вулиця ",
        r"\bул\.\s*": "вулиця ",
        r"\bбуд\.\s*": "",
        r"\bд\.\s*": "",
        r"\bпросп\.\s*": "проспект ",
        r"\bпров\.\s*": "провулок ",
    }
    for pattern, replacement in replacements.items():
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value.strip(" ,")


async def google_geocode(query: str) -> list[dict[str, Any]]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return []
    params = {"address": query, "key": api_key, "language": "uk", "region": "ua"}
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get("https://maps.googleapis.com/maps/api/geocode/json", params=params)
        response.raise_for_status()
        payload = response.json()
    results: list[dict[str, Any]] = []
    for item in payload.get("results", [])[:5]:
        location = item.get("geometry", {}).get("location", {})
        try:
            results.append({
                "lat": float(location["lat"]),
                "lon": float(location["lng"]),
                "label": str(item.get("formatted_address") or query),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return results


async def osm_geocode(query: str) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": 5,
        "countrycodes": "ua",
        "addressdetails": 1,
        "dedupe": 1,
    }
    headers = {
        "User-Agent": base.settings().nominatim_user_agent,
        "Accept-Language": "uk,en;q=0.7",
    }
    async with httpx.AsyncClient(timeout=12, headers=headers) as client:
        response = await client.get("https://nominatim.openstreetmap.org/search", params=params)
        response.raise_for_status()
        payload = response.json()
    return [
        {"lat": float(item["lat"]), "lon": float(item["lon"]), "label": str(item.get("display_name") or query)}
        for item in payload[:5]
    ]


async def receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    if not text:
        return WAIT_ADDRESS

    coordinates = base.parse_coords(text)
    if coordinates:
        lat, lon = coordinates
        context.user_data["point"] = {"lat": lat, "lon": lon, "label": "Введені координати"}
        await ask_azimuth(message, lat, lon)
        return WAIT_AZIMUTH

    url = base.extract_url(text)
    if url:
        coordinates = base.coords_from_url(url)
        if coordinates:
            lat, lon = coordinates
            context.user_data["point"] = {"lat": lat, "lon": lon, "label": "Точка з посилання"}
            await ask_azimuth(message, lat, lon)
            return WAIT_AZIMUTH

    await message.reply_text("🔎 Шукаю точні збіги…", reply_markup=cancel_keyboard())
    query = normalize_address(text)
    try:
        results = await google_geocode(query)
        if not results:
            results = await osm_geocode(query)
    except Exception:
        base.logger.exception("Address geocoding failed")
        results = []

    if not results:
        await message.reply_text(
            "Точку не знайдено. Уточніть населений пункт, вулицю або номер будинку.",
            reply_markup=cancel_keyboard(),
        )
        return WAIT_ADDRESS

    if len(results) == 1:
        point = results[0]
        context.user_data["point"] = point
        await ask_azimuth(message, point["lat"], point["lon"], point["label"])
        return WAIT_AZIMUTH

    context.user_data["candidates"] = results
    buttons = [
        [InlineKeyboardButton(f"{index + 1}. {item['label'][:55]}", callback_data=f"point:{index}")]
        for index, item in enumerate(results)
    ]
    await message.reply_text(
        "Знайдено кілька варіантів. Оберіть потрібну точку:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return WAIT_CHOICE


async def choose_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int((query.data or "").split(":", 1)[1])
        point = context.user_data["candidates"][index]
    except (ValueError, IndexError, KeyError, TypeError):
        await query.edit_message_text("Варіант застарів. Розпочніть пошук знову.")
        return WAIT_ADDRESS

    context.user_data.pop("candidates", None)
    context.user_data["point"] = point
    await query.edit_message_text(f"Обрано: {point['label']}")
    await ask_azimuth(query.message, point["lat"], point["lon"], point["label"])
    return WAIT_AZIMUTH


async def ask_azimuth(message, lat: float, lon: float, label: str | None = None) -> None:
    location_text = f"\n{label}" if label else ""
    await message.reply_text(
        f"Точку визначено: <code>{lat:.7f}, {lon:.7f}</code>{location_text}\n\n"
        "Введіть азимут числом від 0 до 359. Наприклад: <code>45</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )


async def receive_azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:[.,]\d+)?", text):
        await update.effective_message.reply_text(
            "Введіть лише азимут числом від 0 до 359.",
            reply_markup=cancel_keyboard(),
        )
        return WAIT_AZIMUTH

    value = float(text.replace(",", "."))
    if not 0 <= value < 360:
        await update.effective_message.reply_text("Азимут має бути від 0 до 359.", reply_markup=cancel_keyboard())
        return WAIT_AZIMUTH

    update.effective_message.text = str(value)
    result = await base.azimuth(update, context)
    return result


def build_bot():
    application = ApplicationBuilder().token(base.settings().token).concurrent_updates(False).build()

    restart_filter = filters.Regex(f"^{re.escape(BTN_RESTART)}$")
    cancel_filter = filters.Regex(f"^{re.escape(BTN_CANCEL)}$")
    location_button_filter = filters.Regex(f"^{re.escape(BTN_LOCATION)}$")
    address_filter = filters.Regex(f"^{re.escape(BTN_ADDRESS)}$")

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(restart_filter, restart),
        ],
        states={
            WAIT_METHOD: [
                MessageHandler(restart_filter, restart),
                MessageHandler(location_button_filter, wait_for_location),
                MessageHandler(address_filter, ask_address),
            ],
            WAIT_LOCATION: [
                MessageHandler(restart_filter, restart),
                MessageHandler(cancel_filter, cancel),
                MessageHandler(filters.LOCATION, receive_location),
                MessageHandler(filters.ALL, wait_for_location),
            ],
            WAIT_ADDRESS: [
                MessageHandler(restart_filter, restart),
                MessageHandler(cancel_filter, cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address),
            ],
            WAIT_CHOICE: [
                MessageHandler(restart_filter, restart),
                MessageHandler(cancel_filter, cancel),
                CallbackQueryHandler(choose_candidate, pattern=r"^point:\d+$"),
            ],
            WAIT_AZIMUTH: [
                MessageHandler(restart_filter, restart),
                MessageHandler(cancel_filter, cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_azimuth),
            ],
        },
        fallbacks=[
            CommandHandler("start", restart),
            CommandHandler("cancel", cancel),
            MessageHandler(restart_filter, restart),
            MessageHandler(cancel_filter, cancel),
        ],
        allow_reentry=True,
    )
    application.add_handler(conversation)
    return application


base.main_keyboard = initial_keyboard
base.build_bot = build_bot
