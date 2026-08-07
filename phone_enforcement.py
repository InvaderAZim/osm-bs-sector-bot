from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import launcher as bot


_original_build_bot = bot.build_bot


def _has_phone(user_id: int) -> bool:
    row = bot.user_row(user_id)
    return bool(row and (row["phone"] or "").strip())


async def access_gate_with_required_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    bot.upsert_user(user)
    row = bot.user_row(user.id)

    if row and row["status"] == "blocked":
        await message.reply_text(
            "⛔ Доступ скасовано адміністратором.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return False

    if not _has_phone(user.id):
        await message.reply_text(
            "📱 Для користування ботом спочатку надішліть свій номер телефону кнопкою нижче.",
            reply_markup=bot.contact_keyboard(),
        )
        return False

    if bot.is_admin(user.id) or (row and row["status"] == "approved"):
        return True

    await message.reply_text(
        "⏳ Заявка вже надіслана. Очікуйте рішення адміністратора.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return False


async def receive_contact_preserving_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    contact = message.contact if message else None

    if not user or not message or not contact or contact.user_id != user.id:
        if message:
            await message.reply_text(
                "Надішліть саме власний контакт кнопкою нижче.",
                reply_markup=bot.contact_keyboard(),
            )
        return bot.ACCESS

    previous = bot.user_row(user.id)
    previous_status = previous["status"] if previous else "pending"

    bot.upsert_user(user, contact.phone_number)

    if bot.is_admin(user.id) or previous_status == "approved":
        bot.set_status(user.id, "approved")
        await message.reply_text(
            "✅ Номер телефону збережено. Більше запитувати його не будемо.",
            reply_markup=bot.main_keyboard(user.id),
        )
        return bot.MENU

    bot.set_status(user.id, "pending")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{user.id}"),
        InlineKeyboardButton("⛔ Відмовити", callback_data=f"access:block:{user.id}"),
    ]])
    username = f"@{user.username}" if user.username else "без username"

    for admin_id in bot.settings().admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                f"<b>Заявка на доступ</b>\n{user.full_name} · {username}\n"
                f"Телефон: <code>{contact.phone_number}</code>\nID: <code>{user.id}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            bot.log.exception("Failed to notify admin")

    await message.reply_text(
        "✅ Контакт надіслано адміністратору.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return bot.ACCESS


async def force_phone_on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    bot.upsert_user(user)
    row = bot.user_row(user.id)

    if row and row["status"] == "blocked":
        return

    if _has_phone(user.id):
        return

    await message.reply_text(
        "📱 Для користування ботом спочатку надішліть свій номер телефону кнопкою нижче.",
        reply_markup=bot.contact_keyboard(),
    )
    raise ApplicationHandlerStop


async def force_phone_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    bot.upsert_user(user)
    row = bot.user_row(user.id)

    if row and row["status"] == "blocked":
        return

    if _has_phone(user.id):
        return

    await query.answer("Спочатку надішліть номер телефону", show_alert=True)
    if query.message:
        await query.message.reply_text(
            "📱 Для користування ботом спочатку надішліть свій номер телефону кнопкою нижче.",
            reply_markup=bot.contact_keyboard(),
        )
    raise ApplicationHandlerStop


async def global_contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await receive_contact_preserving_access(update, context)
    raise ApplicationHandlerStop


def build_bot_with_phone_requirement():
    application = _original_build_bot()
    application.add_handler(MessageHandler(filters.CONTACT, global_contact_handler), group=-1001)
    application.add_handler(MessageHandler(~filters.CONTACT, force_phone_on_any_message), group=-1000)
    application.add_handler(CallbackQueryHandler(force_phone_on_callback), group=-1000)
    return application


bot.access_gate = access_gate_with_required_phone
bot.receive_contact = receive_contact_preserving_access
bot.build_bot = build_bot_with_phone_requirement
