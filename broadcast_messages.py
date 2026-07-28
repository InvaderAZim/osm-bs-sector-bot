from __future__ import annotations

import logging
import re

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import launcher as bot

log = logging.getLogger("duga-broadcast")

BTN_BROADCAST = "📢 Повідомлення користувачам"
BTN_CANCEL_BROADCAST = "❌ Скасувати розсилку"

_original_main_keyboard = bot.main_keyboard
_original_build_bot = bot.build_bot


def main_keyboard_with_broadcast(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = _original_main_keyboard(user_id)
    if not bot.is_admin(user_id):
        return keyboard

    rows = [list(row) for row in keyboard.keyboard]
    if not any(button.text == BTN_BROADCAST for row in rows for button in row):
        rows.insert(max(0, len(rows) - 1), [KeyboardButton(BTN_BROADCAST)])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not bot.is_admin(user.id):
        raise ApplicationHandlerStop

    context.user_data["awaiting_broadcast"] = True
    await message.reply_text(
        "Введіть текст повідомлення. Бот надішле його всім зареєстрованим користувачам незалежно від статусу.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_CANCEL_BROADCAST)]],
            resize_keyboard=True,
            is_persistent=True,
        ),
    )

    # The same button update must never reach the generic text handler below.
    raise ApplicationHandlerStop


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not bot.is_admin(user.id):
        raise ApplicationHandlerStop

    context.user_data.pop("awaiting_broadcast", None)
    await message.reply_text("Розсилку скасовано.", reply_markup=bot.main_keyboard(user.id))
    raise ApplicationHandlerStop


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if (
        not user
        or not message
        or not bot.is_admin(user.id)
        or not context.user_data.get("awaiting_broadcast")
    ):
        return

    text = (message.text or "").strip()

    # Defensive guard: menu button labels are control commands, never message content.
    if text in {BTN_BROADCAST, BTN_CANCEL_BROADCAST}:
        raise ApplicationHandlerStop

    if not text:
        await message.reply_text("Повідомлення не може бути порожнім.")
        raise ApplicationHandlerStop

    context.user_data.pop("awaiting_broadcast", None)

    with bot.db() as connection:
        rows = connection.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()

    delivered = 0
    failed = 0
    for row in rows:
        target_id = int(row["user_id"])
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"📢 <b>Повідомлення адміністратора</b>\n\n{text}",
                parse_mode="HTML",
            )
            delivered += 1
        except Exception:
            failed += 1
            log.exception("Broadcast failed for user %s", target_id)

    await message.reply_text(
        f"✅ Розсилку завершено.\nДоставлено: {delivered}\nНе доставлено: {failed}",
        reply_markup=bot.main_keyboard(user.id),
    )
    raise ApplicationHandlerStop


def build_bot_with_broadcast():
    app = _original_build_bot()

    # Run control handlers before the ConversationHandler and stop propagation.
    app.add_handler(CommandHandler("broadcast", start_broadcast), group=-210)
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_BROADCAST)}$"), start_broadcast),
        group=-210,
    )
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL_BROADCAST)}$"), cancel_broadcast),
        group=-210,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, send_broadcast),
        group=-200,
    )
    return app


bot.main_keyboard = main_keyboard_with_broadcast
bot.build_bot = build_bot_with_broadcast
