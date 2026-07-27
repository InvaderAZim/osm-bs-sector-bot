from __future__ import annotations

import app as base
from map_preview import create_map_preview
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ConversationHandler


async def azimuth_with_preview(update, context):
    point = context.user_data.get("point")
    parsed = base.parse_azimuth(update.effective_message.text or "")
    if not point or not parsed:
        await update.effective_message.reply_text("Некоректно. Приклад: 90 або 90 15.")
        return base.WAIT_AZIMUTH

    azimuth_value, radius = parsed
    url = base.map_url(point["lat"], point["lon"], azimuth_value, radius, point["label"])
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗺 Відкрити сектор на OpenStreetMap", url=url)]]
    )
    caption = (
        f"<b>Сектор 120° побудовано</b>\n"
        f"Азимут: <b>{azimuth_value:g}°</b>\n"
        f"Радіус: <b>{radius:g} км</b>\n"
        f"Координати БС: <code>{point['lat']:.7f}, {point['lon']:.7f}</code>"
    )

    try:
        image = await create_map_preview(
            lat=point["lat"],
            lon=point["lon"],
            azimuth=azimuth_value,
            radius_km=radius,
            user_agent=base.settings().nominatim_user_agent,
        )
        await update.effective_message.reply_photo(
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        base.logger.exception("Failed to create map preview")
        await update.effective_message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    await update.effective_message.reply_text(
        "Для нового розрахунку натисніть кнопку «Старт» внизу.",
        reply_markup=base.main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


# build_bot() resolves this global when the FastAPI lifespan starts.
base.azimuth = azimuth_with_preview
api = base.api
