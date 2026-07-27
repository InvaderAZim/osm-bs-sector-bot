from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler

import launcher as bot

RADIUS_OPTIONS = (1, 3, 5, 10)


def radius_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 км", callback_data="radius:1"),
            InlineKeyboardButton("3 км", callback_data="radius:3"),
        ],
        [
            InlineKeyboardButton("5 км", callback_data="radius:5"),
            InlineKeyboardButton("10 км", callback_data="radius:10"),
        ],
    ])


async def receive_azimuth(update, context):
    message = update.effective_message
    text = (message.text or "").strip().replace(",", ".") if message else ""

    if not re.fullmatch(r"\d{1,3}(?:\.\d+)?", text):
        await message.reply_text(
            "Введіть лише азимут числом від 0 до 359.",
            reply_markup=bot.cancel_keyboard(),
        )
        return bot.WAIT_AZIMUTH

    azimuth = float(text)
    if not 0 <= azimuth < 360:
        await message.reply_text(
            "Азимут має бути від 0 до 359.",
            reply_markup=bot.cancel_keyboard(),
        )
        return bot.WAIT_AZIMUTH

    if "point" not in context.user_data:
        await message.reply_text("Точка не визначена. Розпочніть новий сектор.")
        return bot.MENU

    context.user_data["azimuth"] = azimuth
    sent = await message.reply_text(
        f"Азимут: <b>{azimuth:g}°</b>\nОберіть радіус візуалізації:",
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )
    context.user_data.setdefault("inline_messages", []).append(sent.message_id)
    return bot.WAIT_AZIMUTH


async def radius_callback(update, context):
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    try:
        radius = int((query.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некоректний радіус", show_alert=True)
        return

    if radius not in RADIUS_OPTIONS:
        await query.answer("Доступні радіуси: 1, 3, 5 або 10 км", show_alert=True)
        return

    point = context.user_data.get("point")
    azimuth = context.user_data.get("azimuth")
    if not point or azimuth is None:
        await query.edit_message_text("Дані запиту застаріли. Створіть новий сектор.")
        return

    await query.edit_message_text(
        f"Обрано радіус: <b>{radius} км</b>. Формую карту…",
        parse_mode=ParseMode.HTML,
    )

    url = bot.map_url(point, float(azimuth), float(radius))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗺 Відкрити на OpenStreetMap", url=url)
    ]])
    caption = (
        f"<b>Сектор 120°</b>\n"
        f"Азимут: <b>{float(azimuth):g}°</b>\n"
        f"Радіус: <b>{radius} км</b>\n"
        f"БС: <code>{point['lat']:.7f}, {point['lon']:.7f}</code>"
    )

    try:
        image = await bot.map_preview(
            float(point["lat"]),
            float(point["lon"]),
            float(azimuth),
            float(radius),
        )
        await query.message.reply_photo(
            photo=image,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        bot.log.exception("Sector preview generation failed")
        await query.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    user_id = query.from_user.id
    await bot.reset_state(update, context)
    await query.message.reply_text(
        "Готово. Оберіть наступну дію:",
        reply_markup=bot.main_keyboard(user_id),
    )


_original_build_bot = bot.build_bot


def build_bot():
    application = _original_build_bot()
    application.add_handler(
        CallbackQueryHandler(radius_callback, pattern=r"^radius:(1|3|5|10)$"),
        group=-17,
    )
    return application


bot.receive_azimuth = receive_azimuth
bot.build_bot = build_bot
