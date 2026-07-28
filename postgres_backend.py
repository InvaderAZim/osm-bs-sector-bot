from __future__ import annotations

import os
import re
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

import launcher as bot

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


class ResultAdapter:
    def __init__(self, cursor: psycopg.Cursor):
        self.cursor = cursor

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class ConnectionAdapter:
    def __init__(self, connection: psycopg.Connection):
        self.connection = connection

    @staticmethod
    def _translate(sql: str) -> str:
        # Existing bot code uses SQLite placeholders. Psycopg requires %s.
        return re.sub(r"\?", "%s", sql)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> ResultAdapter:
        cursor = self.connection.execute(self._translate(sql), tuple(params or ()))
        return ResultAdapter(cursor)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.connection.__exit__(exc_type, exc, tb)


def postgres_db() -> ConnectionAdapter:
    if not DATABASE_URL:
        return _sqlite_db()
    connection = psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
        application_name="duga-telegram-mini-app",
    )
    return ConnectionAdapter(connection)


def postgres_init_db() -> None:
    if not DATABASE_URL:
        _sqlite_init_db()
        return

    with postgres_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'blocked')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC)"
        )
        for admin_id in bot.settings().admin_ids:
            connection.execute(
                """
                INSERT INTO users(user_id, status, created_at, updated_at)
                VALUES(?, 'approved', NOW(), NOW())
                ON CONFLICT(user_id) DO UPDATE
                SET status='approved', updated_at=NOW()
                """,
                (admin_id,),
            )

    bot.log.info("Persistent PostgreSQL user database is active")


_sqlite_db = bot.db
_sqlite_init_db = bot.init_db

# All existing user-management functions resolve these globals at runtime,
# so replacing them here preserves the rest of the bot without a rewrite.
bot.db = postgres_db
bot.init_db = postgres_init_db
