from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.ext import ContextTypes


def parse_ids(value: str) -> frozenset[int]:
    return frozenset(int(item.strip()) for item in value.split(",") if item.strip())


ADMIN_IDS = parse_ids(os.getenv("ADMIN_TELEGRAM_USER_IDS", ""))
STATIC_ALLOWED_IDS = parse_ids(os.getenv("ALLOWED_TELEGRAM_USER_IDS", ""))
DB_PATH = os.getenv("USER_DB_PATH", "/tmp/dugazhtbot-users.db").strip()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone_number TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                usage_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "phone_number" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")
        for user_id in ADMIN_IDS | STATIC_ALLOWED_IDS:
            connection.execute(
                """
                INSERT INTO users(user_id, status, first_seen, last_seen)
                VALUES (?, 'approved', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET status='approved'
                """,
                (user_id, now(), now()),
            )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_user(user_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_status(user_id: int) -> str | None:
    row = get_user(user_id)
    return str(row["status"]) if row else None


def record_user(user: User, increment_usage: bool = False) -> str:
    row = get_user(user.id)
    default_status = "approved" if user.id in ADMIN_IDS | STATIC_ALLOWED_IDS else "pending"
    with connect() as connection:
        if row is None:
            connection.execute(
                """
                INSERT INTO users(user_id, username, first_name, last_name, status, first_seen, last_seen, usage_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user.id, user.username, user.first_name, user.last_name, default_status, now(), now(), 1 if increment_usage else 0),
            )
            return default_status
        connection.execute(
            """
            UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?,
            usage_count=usage_count + ? WHERE user_id=?
            """,
            (user.username, user.first_name, user.last_name, now(), 1 if increment_usage else 0, user.id),
        )
    return str(row["status"])


def save_contact(user: User, phone_number: str) -> None:
    record_user(user)
    with connect() as connection:
        connection.execute(
            "UPDATE users SET phone_number=?, status='pending', last_seen=? WHERE user_id=?",
            (phone_number, now(), user.id),
        )


def set_status(user_id: int, status: str) -> None:
    if status not in {"pending", "approved", "blocked"}:
        raise ValueError("Invalid status")
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, status, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, last_seen=excluded.last_seen
            """,
            (user_id, status, now(), now()),
        )


def has_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS | STATIC_ALLOWED_IDS:
        return True
    return get_status(user_id) == "approved"


def users(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT user_id, username, first_name, last_name, phone_number, status, usage_count, last_seen
            FROM users ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Надіслати свій контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Надішліть власний контакт",
    )


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, user: User, phone_number: str) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approved:{user.id}"),
        InlineKeyboardButton("⛔ Відмовити", callback_data=f"access:blocked:{user.id}"),
    ]])
    username = f"@{user.username}" if user.username else "без username"
    text = (
        "<b>Нова заявка на доступ</b>\n"
        f"{user.full_name} · {username}\n"
        f"Телефон: <code>{phone_number}</code>\n"
        f"Telegram ID: <code>{user.id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except Exception:
            pass


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    record_user(user)
    if has_access(user.id):
        return True
    row = get_user(user.id)
    if update.effective_message:
        if row and row["status"] == "blocked":
            await update.effective_message.reply_text("⛔ У доступі відмовлено адміністратором.")
        elif row and row["phone_number"]:
            await update.effective_message.reply_text("⏳ Заявка вже надіслана. Очікуйте рішення адміністратора.")
        else:
            await update.effective_message.reply_text(
                "🔐 Для отримання доступу надішліть свій контакт кнопкою нижче.",
                reply_markup=contact_keyboard(),
            )
    return False
