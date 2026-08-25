from __future__ import annotations

import hashlib
import os
import time
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from telegram import Update

import launcher as bot
import postgres_backend

TELEGRAM_PATH = "/telegram-webhook"
WEBHOOK_SECRET = hashlib.sha256(bot.settings().secret.encode("utf-8")).hexdigest()
COLD_START_NOTICE_ENABLED = os.getenv("DUGA_COLD_START_NOTICE", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
telegram_application = None
process_started_at = time.monotonic()
cold_start_notice_sent = False


@asynccontextmanager
async def webhook_lifespan(_app):
    global telegram_application, process_started_at, cold_start_notice_sent

    process_started_at = time.monotonic()
    cold_start_notice_sent = False

    bot.validate_settings()
    bot.init_db()

    application = bot.build_bot()
    await application.initialize()
    await application.start()

    webhook_url = f"{bot.settings().public_url}{TELEGRAM_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        max_connections=20,
        secret_token=WEBHOOK_SECRET,
    )

    telegram_application = application
    bot.log.info("Telegram webhook enabled: %s", webhook_url)

    try:
        yield
    finally:
        telegram_application = None
        try:
            await application.stop()
        finally:
            await application.shutdown()
            postgres_backend.close_pool()


async def _send_cold_start_notice(update: Update) -> bool:
    """Optional compatibility notice for sleeping free-tier hosting."""
    global cold_start_notice_sent

    if not COLD_START_NOTICE_ENABLED:
        return False
    if cold_start_notice_sent or time.monotonic() - process_started_at > 90:
        return False

    chat = update.effective_chat
    if chat is None or telegram_application is None:
        return False

    cold_start_notice_sent = True

    if update.callback_query:
        try:
            await update.callback_query.answer("Сервіс запускається…", show_alert=False)
        except Exception:
            pass

    await telegram_application.bot.send_message(
        chat_id=chat.id,
        text=(
            "⏳ Сервіс щойно запускається після паузи.\n\n"
            "Зачекайте приблизно 30 секунд і повторіть дію."
        ),
    )
    return True


@bot.api.post(TELEGRAM_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Bot is starting")

    payload = await request.json()
    update = Update.de_json(payload, telegram_application.bot)

    if await _send_cold_start_notice(update):
        return {"ok": True, "warming_up": True}

    await telegram_application.process_update(update)
    return {"ok": True}


# Webhooks avoid competing getUpdates processes and are the production transport.
bot.api.router.lifespan_context = webhook_lifespan
api = bot.api
