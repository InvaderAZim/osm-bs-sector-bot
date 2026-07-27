from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
WAIT_POINT, WAIT_AZIMUTH = range(2)


class Settings:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        self.secret = os.getenv("MAP_SECRET", "change-me").strip()
        self.radius = float(os.getenv("DEFAULT_RADIUS_KM", "15"))
        raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}
        self.user_agent = os.getenv("NOMINATIM_USER_AGENT", "OSM-BS-Sector-Bot/1.0").strip()

    def validate(self) -> None:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
        if not self.secret or self.secret == "change-me":
            raise RuntimeError("MAP_SECRET is missing")
        if not 0.1 <= self.radius <= 100:
            raise RuntimeError("DEFAULT_RADIUS_KM must be 0.1-100")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not settings().allowed or user.id in settings().allowed)


def valid_coords(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def parse_coordinates(text: str) -> tuple[float, float] | None:
    text = text.strip().replace(";", ",")
    patterns = [
        r"^\s*([+-]?\d{1,2}(?:\.\d+)?)\s*[, ]\s*([+-]?\d{1,3}(?:\.\d+)?)\s*$",
        r"^\s*([+-]?\d{1,2},\d+)\s+([+-]?\d{1,3},\d+)\s*$",
    ]
    for pattern in patterns:
        m = re.match(pattern, text)
        if m:
            lat = float(m.group(1).replace(",", "."))
            lon = float(m.group(2).replace(",", "."))
            if valid_coords(lat, lon):
                return lat, lon
    return None


def parse_url_coordinates(text: str) -> tuple[float, float] | None:
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0).rstrip(".,);]")
    decoded = url.replace("%2C", ",").replace("%2F", "/")
    for pattern in (
        r"@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)",
        r"#map=\d+(?:\.\d+)?/(-?\d{1,2}(?:\.\d+)?)/(-?\d{1,3}(?:\.\d+)?)",
        r"[?&#]q=(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)",
    ):
        found = re.search(pattern, decoded)
        if found:
            lat, lon = float(found.group(1)), float(found.group(2))
            if valid_coords(lat, lon):
                return lat, lon
    return None


async def resolve_google_short_link(text: str) -> tuple[float, float] | None:
    m = re.search(r"https?://(?:maps\.app\.goo\.gl|goo\.gl)/\S+", text)
    if not m:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8, headers={"User-Agent": settings().user_agent}) as client:
            response = await client.get(m.group(0).rstrip(".,);]"))
        return parse_url_coordinates(str(response.url))
    except httpx.HTTPError:
        return None


async def geocode(address: str) -> tuple[float, float, str] | None:
    params = {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "ua", "accept-language": "uk"}
    headers = {"User-Agent": settings().user_agent}
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as client:
            response = await client.get("https://nominatim.openstreetmap.org/search", params=params)
            response.raise_for_status()
            data = response.json()
        if not data:
            return None
        item = data[0]
        return float(item["lat"]), float(item["lon"]), item.get("display_name", address)[:180]
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def resolve_point(text: str) -> tuple[float, float, str] | None:
    pair = parse_coordinates(text) or parse_url_coordinates(text) or await resolve_google_short_link(text)
    if pair:
        return pair[0], pair[1], "Введена точка"
    return await geocode(text)


def parse_azimuth(text: str) -> tuple[float, float] | None:
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if not nums:
        return None
    az = float(nums[0].replace(",", "."))
    radius = float(nums[1].replace(",", ".")) if len(nums) > 1 else settings().radius
    if az == 360:
        az = 0
    if 0 <= az < 360 and 0.1 <= radius <= 100:
        return az, radius
    return None


def map_url(lat: float, lon: float, az: float, radius: float, label: str) -> str:
    serializer = URLSafeSerializer(settings().secret, salt="osm-sector-v1")
    token = serializer.dumps({"lat": round(lat, 7), "lon": round(lon, 7), "az": round(az, 2), "radius": round(radius, 3), "label": label[:180]})
    return f"{settings().base_url}/map/{quote(token, safe='')}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        await update.effective_message.reply_text("Доступ обмежено.")
        return ConversationHandler.END
    context.user_data.clear()
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("📍 Надіслати геолокацію", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.effective_message.reply_text(
        "Надішліть адресу, координати, посилання на карту або геолокацію точки БС.",
        reply_markup=keyboard,
    )
    return WAIT_POINT


async def point_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message.location:
        lat, lon, label = message.location.latitude, message.location.longitude, "Надіслана геолокація"
    elif message.text:
        result = await resolve_point(message.text)
        if not result:
            await message.reply_text("Точку не знайдено. Уточніть адресу або надішліть координати.")
            return WAIT_POINT
        lat, lon, label = result
    else:
        await message.reply_text("Надішліть текст або геолокацію.")
        return WAIT_POINT
    context.user_data["point"] = {"lat": lat, "lon": lon, "label": label}
    await message.reply_text(
        f"Точка БС: <code>{lat:.7f}, {lon:.7f}</code>\nВведіть азимут. Другим числом можна вказати радіус у км, наприклад <code>125 8</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAIT_AZIMUTH


async def azimuth_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    point = context.user_data.get("point")
    parsed = parse_azimuth(update.effective_message.text or "")
    if not point:
        await update.effective_message.reply_text("Сесію втрачено. Натисніть /start.")
        return ConversationHandler.END
    if not parsed:
        await update.effective_message.reply_text("Некоректно. Приклад: 90 або 90 15.")
        return WAIT_AZIMUTH
    az, radius = parsed
    url = map_url(point["lat"], point["lon"], az, radius, point["label"])
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Відкрити сектор OpenStreetMap", url=url)]])
    await update.effective_message.reply_text(
        f"Сектор 120° побудовано.\nАзимут: <b>{az:g}°</b>\nМежі: <b>{(az-60)%360:g}° — {(az+60)%360:g}°</b>\nРадіус: <b>{radius:g} км</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def build_bot():
    bot = ApplicationBuilder().token(settings().token).concurrent_updates(False).build()
    bot.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAIT_POINT: [MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), point_received)],
            WAIT_AZIMUTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, azimuth_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
    ))
    return bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings().validate()
    bot = build_bot()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    try:
        yield
    finally:
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()


api = FastAPI(title="OSM BS Sector Bot", lifespan=lifespan)


@api.get("/")
async def root():
    return {"service": "osm-bs-sector-bot", "status": "ok"}


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/map/{token}", response_class=HTMLResponse)
async def map_page(request: Request, token: str):
    serializer = URLSafeSerializer(settings().secret, salt="osm-sector-v1")
    try:
        data = serializer.loads(token)
        lat, lon = float(data["lat"]), float(data["lon"])
        az, radius = float(data["az"]), float(data["radius"])
    except (BadSignature, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "Недійсне посилання") from exc
    return templates.TemplateResponse("map.html", {
        "request": request,
        "lat": lat,
        "lon": lon,
        "azimuth": az,
        "radius_m": radius * 1000,
        "radius_km": radius,
        "label": str(data.get("label", "БС")),
    })
