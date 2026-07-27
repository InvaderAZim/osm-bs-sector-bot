from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
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
                status TEXT NOT NULL DEFAULT 'pending',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                usage_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
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


def get_status(user_id: int) -> str | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT status FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return str(row["status"]) if row else None


def record_user(user: User, increment_usage: bool = False) -> str:
    current_status = get_status(user.id)
    default_status = "approved" if user.id in ADMIN_IDS | STATIC_ALLOWED_IDS else "pending"
    with connect() as connection:
        if current_status is None:
            connection.execute(
                """
                INSERT INTO users(user_id, username, first_name, last_name, status, first_seen, last_seen, usage_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user.id, user.username, user.first_name, user.last_name,
                 default_status, now(), now(), 1 if increment_usage else 0),
            )
            return default_status
        connection.execute(
            """
            UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?,
            usage_count=usage_count + ? WHERE user_id=?
            """,
            (user.username, user.first_name, user.last_name, now(),
             1 if increment_usage else 0, user.id),
        )
    return current_status


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
    if not ADMIN_IDS and not STATIC_ALLOWED_IDS:
        return True
    if user_id in ADMIN_IDS | STATIC_ALLOWED_IDS:
        return True
    return get_status(user_id) == "approved"


def users(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT user_id, username, first_name, last_name, status, usage_count, last_seen
            FROM users ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, user: User) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approved:{user.id}"),
        InlineKeyboardButton("⛔ Заблокувати", callback_data=f"access:blocked:{user.id}"),
    ]])
    username = f"@{user.username}" if user.username else "без username"
    text = (
        "<b>Нова заявка на доступ</b>\n"
        f"{user.full_name} · {username}\nID: <code>{user.id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except Exception:
            pass


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None:
        return False
    previous = get_status(user.id)
    status = record_user(user)
    if has_access(user.id):
        return True
    if update.effective_message:
        if status == "blocked":
            await update.effective_message.reply_text("⛔ Доступ заблоковано адміністратором.")
        else:
            await update.effective_message.reply_text(
                "🔐 Заявку на доступ передано адміністратору."
            )
    if status == "pending" and previous is None:
        await notify_admins(context, user)
    return False
