from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

import launcher as bot


NOTICE_TEXT = (
    "Шановні користувачі! 🤖 Бот створений із використанням безкоштовних ресурсів, "
    "щоб залишатися повністю безкоштовним, тому інколи можливі тимчасові збої в роботі.\n\n"
    "Якщо бот не відповідає після тривалого простою, натисніть «Запустити DUGA», "
    "зачекайте близько 30 секунд, закрийте додаток і відкрийте його повторно. 🔄\n\n"
    "Дякуємо за розуміння та бажаємо приємної й стабільної роботи з DUGA! 📡"
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
