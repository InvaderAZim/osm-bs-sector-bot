from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import launcher as bot


NOTICE_TEXT = (
    "📡 DUGA оновлено для стабільнішої роботи.\n\n"
    "Якщо під час технічного оновлення сервіс короткочасно не відповідає, "
    "повторіть дію через кілька секунд. 🔄\n\n"
    "Бажаємо зручної роботи з DUGA!"
)

_original_start = bot.start


async def start_with_notice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await _original_start(update, context)

    message = update.effective_message
    if message and message.text and message.text.strip().lower().startswith("/start"):
        try:
            await message.reply_text(NOTICE_TEXT)
        except Exception:
            bot.log.exception("Failed to send start notice")

    return result


bot.start = start_with_notice
