from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

import launcher as bot

PICKER_URL = f"{bot.settings().public_url}/picker"


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(bot.BTN_NEW)],
        [KeyboardButton("🗺 Вибрати точку на карті", web_app=WebAppInfo(url=PICKER_URL))],
        [KeyboardButton(bot.BTN_ADDRESS)],
    ]
    if bot.is_admin(user_id):
        rows.append([KeyboardButton(bot.BTN_USERS)])
    rows.append([KeyboardButton(bot.BTN_RESTART)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


async def receive_map_point(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    web_data = message.web_app_data if message else None
    if message is None or web_data is None:
        return

    if not await bot.access_gate(update, context):
        raise ApplicationHandlerStop

    try:
        payload = json.loads(web_data.data)
        if payload.get("type") != "map_point":
            raise ValueError
        lat = float(payload["lat"])
        lon = float(payload["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
        label = str(payload.get("label") or "Точка, вибрана на карті")[:180]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        await message.reply_text("Не вдалося отримати точку з карти. Спробуйте ще раз.")
        raise ApplicationHandlerStop

    await bot.reset_state(update, context)
    context.user_data["point"] = {"lat": lat, "lon": lon, "label": label}
    context.user_data["map_wait_azimuth"] = True
    await message.reply_text(
        f"Точку визначено: <code>{lat:.7f}, {lon:.7f}</code>\n{label}\n\n"
        "Введіть азимут числом від 0 до 359.",
        parse_mode=ParseMode.HTML,
        reply_markup=bot.cancel_keyboard(),
    )
    raise ApplicationHandlerStop


async def map_azimuth_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("map_wait_azimuth"):
        return

    text = (update.effective_message.text or "").strip()
    if text == bot.BTN_CANCEL:
        context.user_data.pop("map_wait_azimuth", None)
        await bot.show_menu(update, context, "Поточний запит скасовано.")
        raise ApplicationHandlerStop

    result = await bot.receive_azimuth(update, context)
    if result != bot.WAIT_AZIMUTH:
        context.user_data.pop("map_wait_azimuth", None)
    raise ApplicationHandlerStop


_original_build_bot = bot.build_bot


def build_bot():
    application = _original_build_bot()
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_map_point), group=-20)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, map_azimuth_router), group=-19)
    return application


@bot.api.get("/picker", response_class=HTMLResponse)
async def picker_page(request: Request):
    return bot.templates.TemplateResponse("picker.html", {"request": request})


bot.main_keyboard = main_keyboard
bot.build_bot = build_bot
api = bot.api
