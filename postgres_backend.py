from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import launcher as bot

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_POOL: ConnectionPool | None = None
_POOL_LOCK = Lock()


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
        return re.sub(r"\?", "%s", sql)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> ResultAdapter:
        cursor = self.connection.execute(self._translate(sql), tuple(params or ()))
        return ResultAdapter(cursor)


class PooledConnectionContext:
    def __init__(self, pool: ConnectionPool):
        self._context = pool.connection(timeout=5)
        self._connection: psycopg.Connection | None = None

    def __enter__(self) -> ConnectionAdapter:
        self._connection = self._context.__enter__()
        return ConnectionAdapter(self._connection)

    def __exit__(self, exc_type, exc, tb):
        return self._context.__exit__(exc_type, exc, tb)


def _get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ConnectionPool(
                    conninfo=DATABASE_URL,
                    min_size=0,
                    max_size=5,
                    timeout=5,
                    max_idle=60,
                    check=ConnectionPool.check_connection,
                    kwargs={
                        "row_factory": dict_row,
                        "connect_timeout": 5,
                        "application_name": "duga-telegram-mini-app",
                    },
                    open=True,
                    name="duga-postgres-pool",
                )
    return _POOL


def close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


def postgres_db():
    if not DATABASE_URL:
        return _sqlite_db()
    return PooledConnectionContext(_get_pool())


def migrate_sqlite_users(connection: ConnectionAdapter) -> int:
    sqlite_path = Path(bot.settings().db_path)
    if not sqlite_path.exists() or sqlite_path.stat().st_size == 0:
        return 0

    source = None
    try:
        source = sqlite3.connect(sqlite_path)
        source.row_factory = sqlite3.Row
        rows = source.execute("SELECT * FROM users").fetchall()
    except sqlite3.Error:
        bot.log.exception("Could not read the previous SQLite user database")
        return 0
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

    migrated = 0
    for row in rows:
        connection.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, last_name, phone,
                status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?::timestamptz, ?::timestamptz)
            ON CONFLICT(user_id) DO UPDATE SET
                username=EXCLUDED.username,
                first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,
                phone=COALESCE(EXCLUDED.phone, users.phone),
                status=EXCLUDED.status,
                updated_at=GREATEST(users.updated_at, EXCLUDED.updated_at)
            """,
            (
                row["user_id"],
                row["username"],
                row["first_name"],
                row["last_name"],
                row["phone"],
                row["status"],
                row["created_at"],
                row["updated_at"],
            ),
        )
        migrated += 1
    return migrated


def postgres_init_db() -> None:
    if not DATABASE_URL:
        bot.log.warning("DATABASE_URL is missing; using temporary SQLite fallback")
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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC)")

        migrated = migrate_sqlite_users(connection)

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

    bot.log.info(
        "Persistent PostgreSQL user database is active; migrated SQLite users: %s",
        migrated,
    )


_sqlite_db = bot.db
_sqlite_init_db = bot.init_db

bot.db = postgres_db
bot.init_db = postgres_init_db
