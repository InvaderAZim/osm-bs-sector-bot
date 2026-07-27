from __future__ import annotations

import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

import app as base
import simple_flow

BTN_NEW = "🆕 Новий сектор"


def initial_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_NEW)],
        [KeyboardButton(simple_flow.BTN_LOCATION, request_location=True)],
        [KeyboardButton(simple_flow.BTN_ADDRESS)],
    ]
    if simple_flow.is_admin(user_id):
        rows.append([KeyboardButton(simple_flow.BTN_USERS)])
    rows.append([KeyboardButton(simple_flow.BTN_RESTART)])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію",
    )


async def new_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await simple_flow.show_initial_menu(
        update,
        context,
        "Новий сектор. Оберіть спосіб визначення точки БС:",
    )
    raise ApplicationHandlerStop


_original_build_bot = base.build_bot


def build_bot_with_new_sector():
    application = _original_build_bot()
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_NEW)}$"), new_sector),
        group=-10,
    )
    return application


simple_flow.initial_keyboard = initial_keyboard
base.main_keyboard = initial_keyboard
base.build_bot = build_bot_with_new_sector
