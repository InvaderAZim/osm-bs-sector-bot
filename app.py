from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("osm-bs-sector-bot")
WAIT_LOCATION, WAIT_AZIMUTH = range(2)

@dataclass(frozen=True)
class Settings:
    token: str
    public_url: str
    secret: str
    default_radius: float
    allowed_ids: frozenset[int]
    nominatim_user_agent: str

    def validate(self):
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
        if not self.secret:
            raise RuntimeError("MAP_SECRET is missing")

@lru_cache(maxsize=1)
def settings() -> Settings:
    ids = frozenset(int(x.strip()) for x in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if x.strip())
    s = Settings(
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        public_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        secret=os.getenv("MAP_SECRET", "").strip(),
        default_radius=float(os.getenv("DEFAULT_RADIUS_KM", "15")),
        allowed_ids=ids,
        nominatim_user_agent=os.getenv("NOMINATIM_USER_AGENT", "DugaZHTBot/1.0").strip(),
    )
    return s

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

def allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and (not settings().allowed_ids or u.id in settings().allowed_ids)

async def deny(update: Update) -> bool:
    if allowed(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("Доступ обмежено.")
    return True

def valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180

def pair(a: str, b: str):
    try:
        lat, lon = float(a.replace(",", ".")), float(b.replace(",", "."))
    except ValueError:
        return None
    return (lat, lon) if valid(lat, lon) else None

def parse_coords(text: str):
    t = text.strip().strip("()[]{} ")
    for p in (r"^([+-]?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:\.\d+)?)$", r"^([+-]?\d{1,2},\d+)\s+([+-]?\d{1,3},\d+)$"):
        m = re.fullmatch(p, t)
        if m:
            v = pair(m.group(1), m.group(2))
            if v:
                return v
    return None

def extract_url(text: str):
    m = re.search(r"https?://[^\s<>]+", text)
    return m.group(0).rstrip(".,);]") if m else None

def coords_from_url(url: str):
    decoded = url.replace("%2C", ",")
    for p in (r"@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)", r"#map=\d+(?:\.\d+)?/(-?\d{1,2}(?:\.\d+)?)/(-?\d{1,3}(?:\.\d+)?)", r"(?:[?&#]q=)(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)"):
        m = re.search(p, decoded, re.I)
        if m:
            v = pair(m.group(1), m.group(2))
            if v:
                return v
    q = parse_qs(urlparse(decoded).query)
    if "lat" in q and ("lon" in q or "lng" in q):
        return pair(q["lat"][0], (q.get("lon") or q.get("lng"))[0])
    return None

async def resolve_point(text: str):
    c = parse_coords(text)
    if c:
        return c[0], c[1], "Введені координати"
    u = extract_url(text)
    if u:
        c = coords_from_url(u)
        if c:
            return c[0], c[1], "Точка з картографічного посилання"
    params = {"q": text.strip(), "format": "jsonv2", "limit": 1, "countrycodes": "ua", "addressdetails": 1}
    headers = {"User-Agent": settings().nominatim_user_agent}
    try:
        async with httpx.AsyncClient(timeout=12, headers=headers) as client:
            r = await client.get("https://nominatim.openstreetmap.org/search", params=params)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not data:
        return None
    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    return lat, lon, str(data[0].get("display_name") or text)[:160]

def parse_azimuth(text: str):
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if not nums:
        return None
    az = float(nums[0].replace(",", "."))
    radius = float(nums[1].replace(",", ".")) if len(nums) > 1 else settings().default_radius
    if az == 360:
        az = 0
    return (az, radius) if 0 <= az < 360 and .1 <= radius <= 100 else None

def map_url(lat, lon, az, radius, label):
    token = URLSafeSerializer(settings().secret, salt="osm-sector-v1").dumps({"lat": round(lat, 7), "lon": round(lon, 7), "az": az, "radius": radius, "label": label[:160]})
    return f"{settings().public_url}/map/{quote(token, safe='')}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    context.user_data.clear()
    kb = ReplyKeyboardMarkup([[KeyboardButton("📍 Надіслати геолокацію", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.effective_message.reply_text("Надішліть адресу, координати, посилання на карту або геолокацію.", reply_markup=kb)
    return WAIT_LOCATION

async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny(update):
        return ConversationHandler.END
    m = update.effective_message
    if m.location:
        lat, lon, label = m.location.latitude, m.location.longitude, "Надіслана геолокація"
    elif m.text:
        result = await resolve_point(m.text)
        if not result:
            await m.reply_text("Точку не знайдено. Уточніть адресу або надішліть координати.")
            return WAIT_LOCATION
        lat, lon, label = result
    else:
        return WAIT_LOCATION
    context.user_data["point"] = {"lat": lat, "lon": lon, "label": label}
    await m.reply_text(f"БС: <code>{lat:.7f}, {lon:.7f}</code>\nВведіть азимут, наприклад <code>90</code>, або азимут і радіус: <code>90 15</code>.", parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
    return WAIT_AZIMUTH

async def azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = context.user_data.get("point")
    v = parse_azimuth(update.effective_message.text or "")
    if not p or not v:
        await update.effective_message.reply_text("Некоректно. Приклад: 90 або 90 15.")
        return WAIT_AZIMUTH
    az, radius = v
    url = map_url(p["lat"], p["lon"], az, radius, p["label"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Відкрити сектор на OpenStreetMap", url=url)]])
    await update.effective_message.reply_text(f"Сектор 120° побудовано. Азимут: <b>{az:g}°</b>, радіус: <b>{radius:g} км</b>.", parse_mode=ParseMode.HTML, reply_markup=kb)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def build_bot() -> Application:
    app = ApplicationBuilder().token(settings().token).concurrent_updates(False).build()
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("start", start)], states={WAIT_LOCATION: [MessageHandler(filters.LOCATION | (filters.TEXT & ~filters.COMMAND), location)], WAIT_AZIMUTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, azimuth)]}, fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)], allow_reentry=True))
    return app

@asynccontextmanager
async def lifespan(api: FastAPI):
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

api = FastAPI(lifespan=lifespan)

@api.get("/")
async def root():
    return {"service": "OSM BS Sector Bot", "status": "ok"}

@api.head("/")
async def root_head():
    return None

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
    return templates.TemplateResponse(
        request=request,
        name="map.html",
        context={
            "lat": lat,
            "lon": lon,
            "azimuth": az,
            "radius_m": radius * 1000,
            "radius_km": radius,
            "label": str(data.get("label", "БС")),
        },
    )
