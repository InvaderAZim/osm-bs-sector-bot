from __future__ import annotations

import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, MessageHandler, filters

import launcher as bot
import map_picker_v2


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(bot.BTN_NEW)],
        [KeyboardButton("🗺 Вибрати точку на карті", web_app=map_picker_v2.WebAppInfo(url=map_picker_v2.PICKER_URL))],
        [KeyboardButton(bot.BTN_ADDRESS)],
    ]
    if bot.is_admin(user_id):
        rows.append([KeyboardButton(bot.BTN_USERS)])
    rows.append([KeyboardButton(bot.BTN_RESTART)])
    rows.append([KeyboardButton(bot.BTN_CANCEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(bot.BTN_CANCEL)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Натисніть «Скасувати», щоб повернутися назад",
    )


async def cancel_everywhere(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    await bot.reset_state(update, context)
    await update.effective_message.reply_text(
        "Поточну дію скасовано. Повернення до попереднього меню.",
        reply_markup=main_keyboard(update.effective_user.id),
    )
    raise ApplicationHandlerStop


_original_build_bot = bot.build_bot


def build_bot():
    application = _original_build_bot()
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(bot.BTN_CANCEL)}$"), cancel_everywhere),
        group=-100,
    )
    return application


bot.main_keyboard = main_keyboard
bot.cancel_keyboard = cancel_keyboard
map_picker_v2.main_keyboard = main_keyboard
bot.build_bot = build_bot
