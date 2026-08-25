from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

import launcher as bot

log = logging.getLogger("duga-bot.diagnostics")
_original_build_bot = bot.build_bot


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if not bot.has_access(user.id):
        await message.reply_text("⛔ Доступ відсутній. Натисніть /start.")
        return

    await message.reply_text(
        "📡 ДУГА\n\n"
        "1. Оберіть точку або введіть адресу/координати.\n"
        "2. Вкажіть радіус.\n"
        "3. Введіть азимут.\n"
        "4. Отримайте сектор на карті.\n\n"
        "У разі короткого технічного збою повторіть дію через кілька секунд."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if not bot.is_admin(user.id):
        await message.reply_text("⛔ Команда доступна лише адміністратору.")
        return

    db_ok = False
    db_error = None
    try:
        with bot.db() as connection:
            connection.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception as exc:
        db_error = type(exc).__name__
        log.exception("Database status check failed")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "🩺 Стан ДУГА",
        "",
        "✅ Сервер: працює",
        f"{'✅' if db_ok else '❌'} База даних: {'працює' if db_ok else 'помилка'}",
        "✅ Telegram webhook: працює",
        f"🕒 {now}",
    ]
    if db_error:
        lines.append(f"Код помилки БД: {db_error}")
    await message.reply_text("\n".join(lines))


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled Telegram bot error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Сталася тимчасова помилка. Повторіть дію через кілька секунд."
            )
        except Exception:
            log.exception("Failed to send error message to user")


def build_bot_with_diagnostics():
    application = _original_build_bot()
    application.add_handler(CommandHandler("help", help_command), group=-300)
    application.add_handler(CommandHandler("status", status_command), group=-300)
    application.add_error_handler(global_error_handler)
    return application


bot.build_bot = build_bot_with_diagnostics
