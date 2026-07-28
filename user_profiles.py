from __future__ import annotations

from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import launcher as bot


STATUS_LABELS = {
    "approved": "✅ Дозволено",
    "pending": "⏳ Очікує",
    "blocked": "⛔ Заблоковано",
}


def profile_url(row) -> str:
    username = (row["username"] or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"
    return f"tg://user?id={int(row['user_id'])}"


async def users_menu_with_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not bot.is_admin(user.id):
        return bot.MENU

    with bot.db() as connection:
        rows = connection.execute(
            "SELECT * FROM users ORDER BY updated_at DESC, user_id DESC"
        ).fetchall()

    if not rows:
        await message.reply_text("У базі ще немає користувачів.")
        return bot.MENU

    await message.reply_text(f"👥 Усього користувачів: {len(rows)}")

    for row in rows:
        user_id = int(row["user_id"])
        status = row["status"] or "pending"
        username = (row["username"] or "").strip()
        full_name = " ".join(
            part for part in [row["first_name"] or "", row["last_name"] or ""] if part
        ).strip() or "Без імені"

        buttons = [[InlineKeyboardButton("👤 Відкрити профіль", url=profile_url(row))]]

        if user_id not in bot.settings().admin_ids:
            if status == "approved":
                buttons.append([
                    InlineKeyboardButton(
                        "⛔ Скасувати доступ",
                        callback_data=f"manage:revoke:{user_id}",
                    )
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(
                        "✅ Надати доступ",
                        callback_data=f"manage:restore:{user_id}",
                    )
                ])

        role = "Адміністратор" if user_id in bot.settings().admin_ids else "Користувач"
        status_text = "🛡 Адміністратор" if role == "Адміністратор" else STATUS_LABELS.get(status, status)
        username_text = f"@{escape(username.lstrip('@'))}" if username else "не вказано"

        text = (
            f"<b>{escape(full_name)}</b>\n"
            f"Username: <code>{username_text}</code>\n"
            f"Телефон: <code>{escape(str(row['phone'] or 'не надано'))}</code>\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"Статус: <b>{escape(status_text)}</b>"
        )

        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    return bot.MENU


bot.users_menu = users_menu_with_profiles
