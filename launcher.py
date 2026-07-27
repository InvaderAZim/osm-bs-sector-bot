from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import server as preview_server
import user_control

base = preview_server.base
user_control.init_db()


async def controlled_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return not await user_control.ensure_access(update, context)


base.deny = controlled_deny


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()
    if not user_control.is_admin(query.from_user.id):
        await query.answer("Недостатньо прав", show_alert=True)
        return

    try:
        _, status, user_id_raw = (query.data or "").split(":", 2)
        user_id = int(user_id_raw)
        user_control.set_status(user_id, status)
    except (ValueError, TypeError):
        await query.edit_message_text("Некоректна команда керування доступом.")
        return

    label = "✅ Доступ дозволено" if status == "approved" else "⛔ Користувача заблоковано"
    await query.edit_message_text(
        f"{label}\nID: <code>{user_id}</code>", parse_mode=ParseMode.HTML
    )
    try:
        message = (
            "✅ Адміністратор дозволив доступ до бота. Натисніть /start."
            if status == "approved"
            else "⛔ Адміністратор заблокував доступ до бота."
        )
        await context.bot.send_message(user_id, message)
    except Exception:
        pass


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not user_control.is_admin(user.id):
        await update.effective_message.reply_text("Команда доступна лише адміністратору.")
        return

    rows = user_control.users()
    if not rows:
        await update.effective_message.reply_text("Користувачів ще немає.")
        return

    icons = {"approved": "✅", "pending": "⏳", "blocked": "⛔"}
    lines = ["<b>Користувачі бота</b>"]
    for row in rows:
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or "Без імені"
        username = f"@{row['username']}" if row["username"] else "без username"
        lines.append(
            f"{icons.get(row['status'], '•')} <b>{name}</b> · {username}\n"
            f"ID: <code>{row['user_id']}</code> · запусків: {row['usage_count']}"
        )

    text = "\n\n".join(lines)
    for start in range(0, len(text), 3900):
        await update.effective_message.reply_text(
            text[start:start + 3900], parse_mode=ParseMode.HTML
        )


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_command(update, context, "approved")


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_command(update, context, "blocked")


async def set_status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, status: str
) -> None:
    user = update.effective_user
    if user is None or not user_control.is_admin(user.id):
        await update.effective_message.reply_text("Команда доступна лише адміністратору.")
        return
    if not context.args:
        command = "/approve ID" if status == "approved" else "/block ID"
        await update.effective_message.reply_text(f"Формат: {command}")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("ID має бути числом.")
        return
    user_control.set_status(target_id, status)
    await update.effective_message.reply_text(
        f"Статус користувача <code>{target_id}</code> змінено на <b>{status}</b>.",
        parse_mode=ParseMode.HTML,
    )
    try:
        message = (
            "✅ Доступ до бота дозволено. Натисніть /start."
            if status == "approved"
            else "⛔ Доступ до бота заблоковано."
        )
        await context.bot.send_message(target_id, message)
    except Exception:
        pass


_original_build_bot = base.build_bot


def controlled_build_bot():
    application = _original_build_bot()
    application.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"), group=-1)
    application.add_handler(CommandHandler("users", users_command), group=-1)
    application.add_handler(CommandHandler("approve", approve_command), group=-1)
    application.add_handler(CommandHandler("block", block_command), group=-1)
    return application


base.build_bot = controlled_build_bot
api = preview_server.api
