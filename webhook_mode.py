from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

from fastapi import HTTPException, Request
from telegram import Update

import launcher as bot

TELEGRAM_PATH = "/telegram-webhook"
WEBHOOK_SECRET = hashlib.sha256(bot.settings().secret.encode("utf-8")).hexdigest()
telegram_application = None


@asynccontextmanager
async def webhook_lifespan(_app):
    global telegram_application

    bot.validate_settings()
    bot.init_db()

    application = bot.build_bot()
    await application.initialize()
    await application.start()

    webhook_url = f"{bot.settings().public_url}{TELEGRAM_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        secret_token=WEBHOOK_SECRET,
    )

    telegram_application = application
    bot.log.info("Telegram webhook enabled: %s", webhook_url)

    try:
        yield
    finally:
        telegram_application = None
        await application.stop()
        await application.shutdown()


@bot.api.post(TELEGRAM_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Bot is starting")

    payload = await request.json()
    update = Update.de_json(payload, telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}


# Replace the original long-polling lifespan. Webhooks generate inbound traffic,
# wake the free Render service and avoid competing getUpdates processes.
bot.api.router.lifespan_context = webhook_lifespan
api = bot.api
