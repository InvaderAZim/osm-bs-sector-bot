from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import launcher as bot


async def access_gate_with_required_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    bot.upsert_user(user)
    row = bot.user_row(user.id)

    if bot.is_admin(user.id):
        return True

    if row and row["status"] == "blocked":
        await message.reply_text(
            "⛔ Доступ скасовано адміністратором.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return False

    if not row or not (row["phone"] or "").strip():
        await message.reply_text(
            "Для користування ботом надішліть власний номер телефону кнопкою нижче.",
            reply_markup=bot.contact_keyboard(),
        )
        return False

    if row["status"] == "approved":
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

    if bot.is_admin(user.id):
        bot.set_status(user.id, "approved")
        await message.reply_text("✅ Номер телефону збережено.", reply_markup=ReplyKeyboardRemove())
        return bot.MENU

    if previous_status == "approved":
        bot.set_status(user.id, "approved")
        await message.reply_text(
            "✅ Номер телефону збережено. Доступ залишається активним. Натисніть /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return bot.ACCESS

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


bot.access_gate = access_gate_with_required_phone
bot.receive_contact = receive_contact_preserving_access
