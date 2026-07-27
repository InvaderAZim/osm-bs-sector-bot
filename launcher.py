from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import server as preview_server
import ui_buttons
import address_example
import user_control

base = preview_server.base
user_control.init_db()


async def controlled_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return not await user_control.ensure_access(update, context)


base.deny = controlled_deny


async def contact_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    contact = message.contact if message else None
    if user is None or contact is None:
        return

    if user_control.has_access(user.id):
        await message.reply_text(
            "✅ Доступ уже надано. Оберіть дію нижче.",
            reply_markup=ui_buttons.keyboard_for_user(user.id),
        )
        return

    if contact.user_id != user.id:
        await message.reply_text(
            "Потрібно надіслати саме власний контакт через кнопку бота.",
            reply_markup=user_control.contact_keyboard(),
        )
        return

    phone_number = contact.phone_number.strip()
    user_control.save_contact(user, phone_number)
    await user_control.notify_admins(context, user, phone_number)
    await message.reply_text(
        "✅ Контакт отримано. Заявку передано адміністратору. Очікуйте підтвердження.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user is None:
        return
    await query.answer()
    if not user_control.is_admin(query.from_user.id):
        await query.answer("Недостатньо прав", show_alert=True)
        return

    user_control.record_user(query.from_user, increment_usage=True)

    try:
        _, status, user_id_raw = (query.data or "").split(":", 2)
        user_id = int(user_id_raw)
        user_control.set_status(user_id, status)
    except (ValueError, TypeError):
        await query.edit_message_text("Некоректна команда керування доступом.")
        return

    label = "✅ Доступ дозволено" if status == "approved" else "⛔ У доступі відмовлено"
    await query.edit_message_text(
        f"{label}\nID: <code>{user_id}</code>", parse_mode=ParseMode.HTML
    )

    try:
        if status == "approved":
            await context.bot.send_message(
                user_id,
                "✅ Доступ до бота дозволено. Оберіть дію нижче.",
                reply_markup=ui_buttons.keyboard_for_user(user_id),
            )
        else:
            await context.bot.send_message(
                user_id,
                "⛔ Адміністратор відмовив у доступі до бота.",
                reply_markup=ReplyKeyboardRemove(),
            )
    except Exception:
        pass


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not user_control.is_admin(user.id):
        await update.effective_message.reply_text("Команда доступна лише адміністратору.")
        return

    user_control.record_user(user, increment_usage=True)
    rows = user_control.users()
    if not rows:
        await update.effective_message.reply_text("Користувачів ще немає.")
        return

    icons = {"approved": "✅", "pending": "⏳", "blocked": "⛔"}
    lines = ["<b>Користувачі бота</b>"]
    for row in rows:
        name = " ".join(filter(None, [row["first_name"], row["last_name"]])) or "Без імені"
        username = f"@{row['username']}" if row["username"] else "без username"
        phone = row["phone_number"] or "контакт не надано"
        admin_mark = " · 👑 адміністратор" if user_control.is_admin(row["user_id"]) else ""
        lines.append(
            f"{icons.get(row['status'], '•')} <b>{name}</b> · {username}{admin_mark}\n"
            f"Телефон: <code>{phone}</code>\n"
            f"ID: <code>{row['user_id']}</code> · дій: {row['usage_count']}"
        )

    text = "\n\n".join(lines)
    for start in range(0, len(text), 3900):
        await update.effective_message.reply_text(
            text[start:start + 3900],
            parse_mode=ParseMode.HTML,
            reply_markup=ui_buttons.admin_keyboard(),
        )


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_command(update, context, "approved")


async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_status_command(update, context, "blocked")


async def set_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str) -> None:
    user = update.effective_user
    if user is None or not user_control.is_admin(user.id):
        await update.effective_message.reply_text("Команда доступна лише адміністратору.")
        return

    user_control.record_user(user, increment_usage=True)

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
        reply_markup=ui_buttons.admin_keyboard(),
    )

    try:
        if status == "approved":
            await context.bot.send_message(
                target_id,
                "✅ Доступ до бота дозволено. Оберіть дію нижче.",
                reply_markup=ui_buttons.keyboard_for_user(target_id),
            )
        else:
            await context.bot.send_message(
                target_id,
                "⛔ У доступі до бота відмовлено.",
                reply_markup=ReplyKeyboardRemove(),
            )
    except Exception:
        pass


_original_build_bot = base.build_bot


def controlled_build_bot():
    application = _original_build_bot()
    application.add_handler(MessageHandler(filters.CONTACT, contact_request), group=-2)
    application.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"), group=-1)
    application.add_handler(CommandHandler("users", users_command), group=-1)
    application.add_handler(MessageHandler(filters.Regex(f"^{ui_buttons.BTN_USERS}$"), users_command), group=-1)
    application.add_handler(CommandHandler("approve", approve_command), group=-1)
    application.add_handler(CommandHandler("block", block_command), group=-1)
    return application


base.build_bot = controlled_build_bot
api = preview_server.api
