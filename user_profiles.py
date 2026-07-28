from __future__ import annotations

import csv
from datetime import datetime, timezone
from html import escape
from io import BytesIO, StringIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes

import launcher as bot


STATUS_LABELS = {
    "approved": "✅ Дозволено",
    "pending": "⏳ Очікує дозволу",
    "blocked": "⛔ Заблоковано",
}

_original_build_bot = bot.build_bot


def category_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏳ Потребують дозволу · {counts['pending']}", callback_data="users:list:pending")],
        [InlineKeyboardButton(f"✅ Надано доступ · {counts['approved']}", callback_data="users:list:approved")],
        [InlineKeyboardButton(f"⛔ Заблоковані · {counts['blocked']}", callback_data="users:list:blocked")],
        [InlineKeyboardButton("📋 Завантажити список користувачів", callback_data="users:export")],
    ])


def get_counts() -> dict[str, int]:
    with bot.db() as connection:
        rows = connection.execute(
            """
            SELECT
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked
            FROM users
            """
        ).fetchone()
    return {
        "pending": int(rows["pending"] or 0),
        "approved": int(rows["approved"] or 0),
        "blocked": int(rows["blocked"] or 0),
    }


async def users_menu_with_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not bot.is_admin(user.id):
        return bot.MENU

    await message.reply_text(
        "👥 <b>Керування користувачами</b>\n\nОберіть категорію:",
        parse_mode=ParseMode.HTML,
        reply_markup=category_keyboard(get_counts()),
    )
    return bot.MENU


def rows_for_category(category: str):
    with bot.db() as connection:
        return connection.execute(
            "SELECT * FROM users WHERE status=? ORDER BY updated_at DESC, user_id DESC",
            (category,),
        ).fetchall()


def all_user_rows():
    with bot.db() as connection:
        return connection.execute(
            "SELECT * FROM users ORDER BY status, updated_at DESC, user_id DESC"
        ).fetchall()


def user_buttons(row, category: str) -> InlineKeyboardMarkup | None:
    user_id = int(row["user_id"])
    username = (row["username"] or "").strip().lstrip("@")
    buttons = []

    if username:
        buttons.append([InlineKeyboardButton("👤 Відкрити профіль", url=f"https://t.me/{username}")])
    else:
        buttons.append([InlineKeyboardButton("👤 Відкрити профіль", callback_data=f"profile:open:{user_id}")])

    if user_id not in bot.settings().admin_ids:
        if category == "pending":
            buttons.append([
                InlineKeyboardButton("✅ Надати доступ", callback_data=f"manage:restore:{user_id}"),
                InlineKeyboardButton("⛔ Заблокувати", callback_data=f"manage:revoke:{user_id}"),
            ])
        elif category == "approved":
            buttons.append([InlineKeyboardButton("⛔ Скасувати доступ", callback_data=f"manage:revoke:{user_id}")])
        elif category == "blocked":
            buttons.append([InlineKeyboardButton("✅ Відновити доступ", callback_data=f"manage:restore:{user_id}")])

    return InlineKeyboardMarkup(buttons) if buttons else None


async def export_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not bot.is_admin(query.from_user.id):
        return

    await query.answer("Формую список…")
    rows = all_user_rows()

    text_buffer = StringIO(newline="")
    writer = csv.writer(text_buffer, delimiter=";")
    writer.writerow(["Ім'я", "Username", "Телефон", "Telegram ID", "Статус", "Створено", "Оновлено"])

    for row in rows:
        full_name = " ".join(
            part for part in [row["first_name"] or "", row["last_name"] or ""] if part
        ).strip() or "Без імені"
        username = (row["username"] or "").strip()
        if username and not username.startswith("@"):
            username = f"@{username}"
        writer.writerow([
            full_name,
            username,
            row["phone"] or "",
            int(row["user_id"]),
            STATUS_LABELS.get(row["status"], row["status"]),
            str(row["created_at"] or ""),
            str(row["updated_at"] or ""),
        ])

    payload = ("\ufeff" + text_buffer.getvalue()).encode("utf-8")
    document = BytesIO(payload)
    document.name = f"DUGA_users_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M')}.csv"

    await query.message.reply_document(
        document=document,
        filename=document.name,
        caption=f"📋 Список користувачів DUGA\nКількість: {len(rows)}",
    )
    raise ApplicationHandlerStop


async def open_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not bot.is_admin(query.from_user.id):
        return

    await query.answer()
    user_id = int(query.data.rsplit(":", 1)[-1])
    row = bot.user_row(user_id)
    if not row:
        await query.message.reply_text("Користувача не знайдено в базі.")
        raise ApplicationHandlerStop

    username = (row["username"] or "").strip().lstrip("@")
    if username:
        await query.message.reply_text(
            f"👤 <a href=\"https://t.me/{escape(username)}\">Відкрити профіль користувача</a>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        full_name = " ".join(
            part for part in [row["first_name"] or "", row["last_name"] or ""] if part
        ).strip() or "Користувач"
        await query.message.reply_text(
            f"👤 <a href=\"tg://user?id={user_id}\">{escape(full_name)}</a>\n"
            "Натисніть на ім’я, щоб відкрити профіль.",
            parse_mode=ParseMode.HTML,
        )
    raise ApplicationHandlerStop


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not bot.is_admin(query.from_user.id):
        return

    await query.answer()
    category = query.data.rsplit(":", 1)[-1]
    labels = {
        "pending": "⏳ Потребують дозволу",
        "approved": "✅ Користувачі з доступом",
        "blocked": "⛔ Заблоковані користувачі",
    }
    if category not in labels:
        return

    rows = rows_for_category(category)
    await query.edit_message_text(
        f"<b>{labels[category]}</b>\nКількість: <b>{len(rows)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ До категорій", callback_data="users:categories")]]),
    )

    if not rows:
        await query.message.reply_text("У цій категорії користувачів немає.")
        raise ApplicationHandlerStop

    for row in rows:
        user_id = int(row["user_id"])
        username = (row["username"] or "").strip()
        full_name = " ".join(part for part in [row["first_name"] or "", row["last_name"] or ""] if part).strip() or "Без імені"
        role = "🛡 Адміністратор" if user_id in bot.settings().admin_ids else STATUS_LABELS.get(category, category)
        username_text = f"@{escape(username.lstrip('@'))}" if username else "не вказано"

        text = (
            f"<b>{escape(full_name)}</b>\n"
            f"Username: <code>{username_text}</code>\n"
            f"Телефон: <code>{escape(str(row['phone'] or 'не надано'))}</code>\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"Статус: <b>{escape(role)}</b>"
        )
        await query.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=user_buttons(row, category),
            disable_web_page_preview=True,
        )

    raise ApplicationHandlerStop


async def show_categories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not bot.is_admin(query.from_user.id):
        return
    await query.answer()
    await query.edit_message_text(
        "👥 <b>Керування користувачами</b>\n\nОберіть категорію:",
        parse_mode=ParseMode.HTML,
        reply_markup=category_keyboard(get_counts()),
    )
    raise ApplicationHandlerStop


def build_bot_with_user_categories():
    application = _original_build_bot()
    application.add_handler(CallbackQueryHandler(show_category, pattern=r"^users:list:(pending|approved|blocked)$"), group=-170)
    application.add_handler(CallbackQueryHandler(show_categories_callback, pattern=r"^users:categories$"), group=-170)
    application.add_handler(CallbackQueryHandler(export_users_callback, pattern=r"^users:export$"), group=-170)
    application.add_handler(CallbackQueryHandler(open_profile_callback, pattern=r"^profile:open:\d+$"), group=-170)
    return application


bot.users_menu = users_menu_with_profiles
bot.build_bot = build_bot_with_user_categories
