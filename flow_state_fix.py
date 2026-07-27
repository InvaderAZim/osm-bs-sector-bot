from __future__ import annotations

import json

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

import app as base
import ui_buttons


async def web_app_point(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    data = message.web_app_data.data if message and message.web_app_data else ""
    if not data:
        return

    try:
        payload = json.loads(data)
        if payload.get("type") != "map_point":
            return
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        await message.reply_text("Не вдалося отримати точку з карти. Спробуйте ще раз.")
        raise ApplicationHandlerStop

    if not base.valid(lat, lon):
        await message.reply_text("Отримано некоректні координати.")
        raise ApplicationHandlerStop

    context.user_data["point"] = {
        "lat": lat,
        "lon": lon,
        "label": "Точка, вибрана на карті",
    }
    await message.reply_text(
        f"БС: <code>{lat:.7f}, {lon:.7f}</code>\n"
        "Оберіть азимут або введіть власне значення.",
        parse_mode=ParseMode.HTML,
        reply_markup=ui_buttons.azimuth_keyboard(),
    )
    raise ApplicationHandlerStop


async def route_azimuth_when_point_exists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    if not text or "point" not in context.user_data:
        return

    normalized = ui_buttons.normalize_azimuth_button(text)
    if base.parse_azimuth(normalized) is None:
        return

    message.text = normalized
    await base.azimuth(update, context)
    raise ApplicationHandlerStop


_original_build_bot = base.build_bot


def build_bot_with_state_fix():
    application = _original_build_bot()
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_point), group=-5)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_azimuth_when_point_exists), group=-4)
    return application


base.build_bot = build_bot_with_state_fix
