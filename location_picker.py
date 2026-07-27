from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

import app as base
import ui_buttons


PICKER_URL = f"{base.settings().public_url}/picker"


@base.api.get("/picker", response_class=HTMLResponse)
async def picker_page(request: Request):
    return base.templates.TemplateResponse(
        request=request,
        name="picker.html",
        context={},
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(ui_buttons.BTN_LOCATION, web_app=WebAppInfo(url=PICKER_URL))],
            [KeyboardButton(ui_buttons.BTN_MANUAL)],
            [KeyboardButton(ui_buttons.BTN_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть точку на карті або введіть адресу",
    )


async def receive_map_point(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    web_data = message.web_app_data if message else None
    if message is None or user is None or web_data is None:
        return

    if await base.deny(update, context):
        raise ApplicationHandlerStop

    try:
        payload = json.loads(web_data.data)
        if payload.get("type") != "map_point":
            raise ValueError
        lat = float(payload["lat"])
        lon = float(payload["lon"])
        if not base.valid(lat, lon):
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        await message.reply_text("Не вдалося отримати точку з карти. Спробуйте ще раз.")
        raise ApplicationHandlerStop

    context.user_data["point"] = {
        "lat": lat,
        "lon": lon,
        "label": "Точка, вибрана на карті",
    }
    context.user_data["picker_wait_azimuth"] = True

    await message.reply_text(
        f"БС: <code>{lat:.7f}, {lon:.7f}</code>\nОберіть азимут або введіть власне значення.",
        parse_mode=ParseMode.HTML,
        reply_markup=ui_buttons.azimuth_keyboard(),
    )
    raise ApplicationHandlerStop


async def picker_azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("picker_wait_azimuth"):
        return

    text = (update.effective_message.text or "").strip()
    if text == ui_buttons.BTN_CANCEL:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Дію скасовано.",
            reply_markup=ui_buttons.keyboard_for_user(update.effective_user.id if update.effective_user else None),
        )
        raise ApplicationHandlerStop

    if text == ui_buttons.BTN_BACK:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Оберіть точку на карті або введіть адресу.",
            reply_markup=location_keyboard(),
        )
        raise ApplicationHandlerStop

    if text == ui_buttons.BTN_CUSTOM_AZ:
        await ui_buttons.custom_azimuth_prompt(update, context)
        raise ApplicationHandlerStop

    normalized = ui_buttons.normalize_azimuth_button(text)
    update.effective_message.text = normalized
    result = await base.azimuth(update, context)
    if result == base.WAIT_AZIMUTH:
        context.user_data["picker_wait_azimuth"] = True
    else:
        context.user_data.pop("picker_wait_azimuth", None)
    raise ApplicationHandlerStop


_original_build_bot = base.build_bot


def build_bot_with_picker():
    application = _original_build_bot()
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receive_map_point), group=-4)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, picker_azimuth), group=-4)
    return application


ui_buttons.location_keyboard = location_keyboard
base.location_keyboard = location_keyboard
base.build_bot = build_bot_with_picker
