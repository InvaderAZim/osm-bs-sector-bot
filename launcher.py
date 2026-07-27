from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from PIL import Image, ImageDraw
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("duga-bot")

ACCESS, MENU, WAIT_LOCATION, WAIT_ADDRESS, WAIT_CHOICE, WAIT_AZIMUTH = range(6)
BTN_NEW = "🆕 Новий сектор"
BTN_LOCATION = "📍 Надіслати геолокацію"
BTN_ADDRESS = "⌨️ Ввести адресу або координати"
BTN_CANCEL = "❌ Скасувати"
BTN_RESTART = "🔄 Перезапустити бота"
BTN_USERS = "👥 Користувачі"
BTN_CONTACT = "📱 Надіслати свій контакт"

TILE_SIZE = 256
EARTH_RADIUS_M = 6371008.8


@dataclass(frozen=True)
class Settings:
    token: str
    public_url: str
    secret: str
    default_radius_km: float
    admin_ids: frozenset[int]
    google_key: str
    nominatim_agent: str
    db_path: str


@lru_cache(maxsize=1)
def settings() -> Settings:
    parse_ids = lambda value: frozenset(int(x.strip()) for x in value.split(",") if x.strip())
    return Settings(
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        public_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        secret=os.getenv("MAP_SECRET", "").strip(),
        default_radius_km=float(os.getenv("DEFAULT_RADIUS_KM", "15")),
        admin_ids=parse_ids(os.getenv("ADMIN_TELEGRAM_USER_IDS", "")),
        google_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
        nominatim_agent=os.getenv("NOMINATIM_USER_AGENT", "DugaZHTBot/2.0").strip(),
        db_path=os.getenv("USER_DB_PATH", "/tmp/duga-users.db").strip(),
    )


def validate_settings() -> None:
    cfg = settings()
    if not cfg.token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not cfg.secret:
        raise RuntimeError("MAP_SECRET is missing")
    if not cfg.admin_ids:
        raise RuntimeError("ADMIN_TELEGRAM_USER_IDS is missing")


def db() -> sqlite3.Connection:
    path = Path(settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    with db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        for admin_id in settings().admin_ids:
            connection.execute("""
                INSERT INTO users(user_id,status,created_at,updated_at)
                VALUES(?, 'approved', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET status='approved', updated_at=excluded.updated_at
            """, (admin_id, now(), now()))


def upsert_user(user, phone: str | None = None) -> None:
    status = "approved" if user.id in settings().admin_ids else "pending"
    with db() as connection:
        connection.execute("""
            INSERT INTO users(user_id,username,first_name,last_name,phone,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                phone=COALESCE(excluded.phone,users.phone),
                updated_at=excluded.updated_at
        """, (user.id, user.username, user.first_name, user.last_name, phone, status, now(), now()))


def user_row(user_id: int):
    with db() as connection:
        return connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_status(user_id: int, status: str) -> None:
    with db() as connection:
        connection.execute("UPDATE users SET status=?, updated_at=? WHERE user_id=?", (status, now(), user_id))


def has_access(user_id: int) -> bool:
    if user_id in settings().admin_ids:
        return True
    row = user_row(user_id)
    return bool(row and row["status"] == "approved")


def is_admin(user_id: int) -> bool:
    return user_id in settings().admin_ids


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_CONTACT, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_NEW)],
        [KeyboardButton(BTN_LOCATION, request_location=True)],
        [KeyboardButton(BTN_ADDRESS)],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(BTN_USERS)])
    rows.append([KeyboardButton(BTN_RESTART)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True, is_persistent=True)


async def remove_old_inline_menus(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.user_data.get("chat_id")
    for message_id in context.user_data.get("inline_messages", []):
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass


async def reset_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await remove_old_inline_menus(context)
    context.user_data.clear()
    if update.effective_chat:
        context.user_data["chat_id"] = update.effective_chat.id


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    upsert_user(user)
    if has_access(user.id):
        return True
    row = user_row(user.id)
    if row and row["status"] == "blocked":
        await update.effective_message.reply_text("⛔ Доступ скасовано адміністратором.", reply_markup=ReplyKeyboardRemove())
    elif row and row["phone"]:
        await update.effective_message.reply_text("⏳ Заявка вже надіслана. Очікуйте рішення адміністратора.", reply_markup=ReplyKeyboardRemove())
    else:
        await update.effective_message.reply_text("Для доступу надішліть власний контакт.", reply_markup=contact_keyboard())
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await access_gate(update, context):
        return ACCESS
    await reset_state(update, context)
    user_id = update.effective_user.id
    await update.effective_message.reply_text("Оберіть дію:", reply_markup=main_keyboard(user_id))
    return MENU


async def receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    contact = update.effective_message.contact
    if not user or not contact or contact.user_id != user.id:
        await update.effective_message.reply_text("Надішліть саме власний контакт кнопкою нижче.", reply_markup=contact_keyboard())
        return ACCESS
    upsert_user(user, contact.phone_number)
    set_status(user.id, "pending")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Дозволити", callback_data=f"access:approve:{user.id}"),
        InlineKeyboardButton("⛔ Відмовити", callback_data=f"access:block:{user.id}"),
    ]])
    username = f"@{user.username}" if user.username else "без username"
    for admin_id in settings().admin_ids:
        try:
            await context.bot.send_message(
                admin_id,
                f"<b>Заявка на доступ</b>\n{user.full_name} · {username}\nТелефон: <code>{contact.phone_number}</code>\nID: <code>{user.id}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            log.exception("Failed to notify admin")
    await update.effective_message.reply_text("✅ Контакт надіслано адміністратору.", reply_markup=ReplyKeyboardRemove())
    return ACCESS


async def access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or not is_admin(query.from_user.id):
        return
    await query.answer()
    _, action, user_id_text = query.data.split(":", 2)
    user_id = int(user_id_text)
    status = "approved" if action == "approve" else "blocked"
    set_status(user_id, status)
    await query.edit_message_text(f"{'✅ Доступ дозволено' if status == 'approved' else '⛔ Доступ відхилено'}\nID: <code>{user_id}</code>", parse_mode=ParseMode.HTML)
    try:
        if status == "approved":
            await context.bot.send_message(user_id, "✅ Доступ дозволено. Натисніть /start.")
        else:
            await context.bot.send_message(user_id, "⛔ У доступі відмовлено.")
    except Exception:
        pass


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Оберіть дію:"):
    if not await access_gate(update, context):
        return ACCESS
    await reset_state(update, context)
    await update.effective_message.reply_text(text, reply_markup=main_keyboard(update.effective_user.id))
    return MENU


async def choose_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_state(update, context)
    await update.effective_message.reply_text(
        "Надішліть геолокацію кнопкою нижче.",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton(BTN_LOCATION, request_location=True)],
            [KeyboardButton(BTN_CANCEL)],
        ], resize_keyboard=True, is_persistent=True),
    )
    return WAIT_LOCATION


async def receive_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.effective_message.location
    if not location:
        await update.effective_message.reply_text("Очікую саме геолокацію.", reply_markup=cancel_keyboard())
        return WAIT_LOCATION
    context.user_data["point"] = {"lat": location.latitude, "lon": location.longitude, "label": "Надіслана геолокація"}
    await ask_azimuth(update, context)
    return WAIT_AZIMUTH


async def choose_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_state(update, context)
    await update.effective_message.reply_text(
        "Введіть адресу в будь-якому форматі або координати.\nПриклад: <code>Житомир Грушевського 5</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )
    return WAIT_ADDRESS


def parse_coordinates(text: str):
    match = re.fullmatch(r"\s*([+-]?\d{1,2}(?:[.,]\d+)?)\s*[,;\s]\s*([+-]?\d{1,3}(?:[.,]\d+)?)\s*", text)
    if not match:
        return None
    lat = float(match.group(1).replace(",", "."))
    lon = float(match.group(2).replace(",", "."))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def normalize_address(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    substitutions = [
        (r"\bм\.\s*", ""), (r"\bвул\.\s*", "вулиця "), (r"\bул\.\s*", "вулиця "),
        (r"\bбуд\.\s*", ""), (r"\bд\.\s*", ""), (r"\bпросп\.\s*", "проспект "),
    ]
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text.strip(" ,")


async def geocode(text: str) -> list[dict[str, Any]]:
    query = normalize_address(text)
    cfg = settings()
    results: list[dict[str, Any]] = []
    if cfg.google_key:
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get("https://maps.googleapis.com/maps/api/geocode/json", params={
                    "address": query, "key": cfg.google_key, "language": "uk", "region": "ua"
                })
                response.raise_for_status()
                for item in response.json().get("results", [])[:5]:
                    point = item["geometry"]["location"]
                    results.append({"lat": float(point["lat"]), "lon": float(point["lng"]), "label": item.get("formatted_address", query)})
        except Exception:
            log.exception("Google geocoding failed")
    if results:
        return results
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": cfg.nominatim_agent, "Accept-Language": "uk,en;q=0.7"}) as client:
        response = await client.get("https://nominatim.openstreetmap.org/search", params={
            "q": query, "format": "jsonv2", "limit": 5, "countrycodes": "ua", "addressdetails": 1
        })
        response.raise_for_status()
        return [{"lat": float(x["lat"]), "lon": float(x["lon"]), "label": x.get("display_name", query)} for x in response.json()[:5]]


async def receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    coords = parse_coordinates(text)
    if coords:
        context.user_data["point"] = {"lat": coords[0], "lon": coords[1], "label": "Введені координати"}
        await ask_azimuth(update, context)
        return WAIT_AZIMUTH
    await update.effective_message.reply_text("🔎 Шукаю точку…", reply_markup=cancel_keyboard())
    try:
        results = await geocode(text)
    except Exception:
        log.exception("Geocoding failed")
        results = []
    if not results:
        await update.effective_message.reply_text("Точку не знайдено. Уточніть адресу.", reply_markup=cancel_keyboard())
        return WAIT_ADDRESS
    if len(results) == 1:
        context.user_data["point"] = results[0]
        await ask_azimuth(update, context)
        return WAIT_AZIMUTH
    context.user_data["candidates"] = results
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{i+1}. {item['label'][:55]}", callback_data=f"point:{i}")]
        for i, item in enumerate(results)
    ])
    sent = await update.effective_message.reply_text("Знайдено кілька точок. Оберіть потрібну:", reply_markup=keyboard)
    context.user_data.setdefault("inline_messages", []).append(sent.message_id)
    return WAIT_CHOICE


async def choose_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    index = int(query.data.split(":", 1)[1])
    point = context.user_data["candidates"][index]
    context.user_data["point"] = point
    context.user_data.pop("candidates", None)
    await query.edit_message_text(f"Обрано: {point['label']}")
    await ask_azimuth(update, context)
    return WAIT_AZIMUTH


async def ask_azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    point = context.user_data["point"]
    message = update.effective_message or update.callback_query.message
    await message.reply_text(
        f"Точку визначено: <code>{point['lat']:.7f}, {point['lon']:.7f}</code>\n\nВведіть азимут числом від 0 до 359.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )


def destination(lat: float, lon: float, bearing: float, distance_m: float):
    phi1, lam1 = math.radians(lat), math.radians(lon)
    theta, delta = math.radians(bearing), distance_m / EARTH_RADIUS_M
    phi2 = math.asin(math.sin(phi1)*math.cos(delta)+math.cos(phi1)*math.sin(delta)*math.cos(theta))
    lam2 = lam1 + math.atan2(math.sin(theta)*math.sin(delta)*math.cos(phi1), math.cos(delta)-math.sin(phi1)*math.sin(phi2))
    return math.degrees(phi2), ((math.degrees(lam2)+540)%360)-180


def world(lat: float, lon: float, zoom: int):
    scale = TILE_SIZE * 2**zoom
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180) / 360 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1+s)/(1-s))/(4*math.pi)) * scale
    return x, y


async def map_preview(lat: float, lon: float, azimuth: float, radius_km: float) -> BytesIO:
    width, height, zoom = 900, 600, 11 if radius_km <= 16 else 10
    cx, cy = world(lat, lon, zoom)
    left, top = cx-width/2, cy-height/2
    canvas = Image.new("RGB", (width, height), (235,235,235))
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": settings().nominatim_agent}) as client:
        tasks = []
        for tx in range(math.floor(left/256), math.floor((left+width)/256)+1):
            for ty in range(math.floor(top/256), math.floor((top+height)/256)+1):
                tasks.append((tx,ty,client.get(f"https://tile.openstreetmap.org/{zoom}/{tx%(2**zoom)}/{ty}.png")))
        for tx,ty,request_task in tasks:
            try:
                response = await request_task
                response.raise_for_status()
                tile = Image.open(BytesIO(response.content)).convert("RGB")
                canvas.paste(tile, (round(tx*256-left), round(ty*256-top)))
            except Exception:
                pass
    overlay = Image.new("RGBA", canvas.size, (0,0,0,0))
    draw = ImageDraw.Draw(overlay)
    center = (width/2, height/2)
    def point(bearing, distance):
        plat, plon = destination(lat, lon, bearing, distance)
        x,y = world(plat, plon, zoom)
        return x-left, y-top
    for step in range(24,0,-1):
        fraction = step/24
        polygon = [center] + [point(azimuth+offset, radius_km*1000*fraction) for offset in range(-60,61,3)]
        draw.polygon(polygon, fill=(220,38,38,round(12+65*(1-fraction))))
    left_edge, right_edge = point(azimuth-60,radius_km*1000), point(azimuth+60,radius_km*1000)
    arc = [point(azimuth+offset,radius_km*1000) for offset in range(-60,61,2)]
    draw.line([center,left_edge],fill=(190,20,20,255),width=5)
    draw.line([center,right_edge],fill=(190,20,20,255),width=5)
    draw.line(arc,fill=(220,38,38,220),width=4)
    draw.ellipse((width/2-10,height/2-10,width/2+10,height/2+10),fill=(239,68,68,255),outline=(100,0,0,255),width=3)
    draw.rectangle((0,height-28,width,height),fill=(255,255,255,220))
    draw.text((10,height-22),"© OpenStreetMap contributors",fill=(20,20,20,255))
    output = BytesIO()
    Image.alpha_composite(canvas.convert("RGBA"),overlay).convert("RGB").save(output,"JPEG",quality=90)
    output.seek(0)
    output.name = "sector.jpg"
    return output


def map_url(point: dict[str, Any], azimuth: float, radius: float):
    token = URLSafeSerializer(settings().secret, salt="sector-v2").dumps({
        "lat": point["lat"], "lon": point["lon"], "az": azimuth, "radius": radius
    })
    return f"{settings().public_url}/map/{quote(token, safe='')}"


async def receive_azimuth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,3}(?:\.\d+)?", text):
        await update.effective_message.reply_text("Введіть лише число від 0 до 359.", reply_markup=cancel_keyboard())
        return WAIT_AZIMUTH
    azimuth = float(text)
    if not 0 <= azimuth < 360:
        await update.effective_message.reply_text("Азимут має бути від 0 до 359.", reply_markup=cancel_keyboard())
        return WAIT_AZIMUTH
    point = context.user_data["point"]
    radius = settings().default_radius_km
    url = map_url(point, azimuth, radius)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🗺 Відкрити на OpenStreetMap", url=url)]])
    caption = f"<b>Сектор 120°</b>\nАзимут: <b>{azimuth:g}°</b>\nРадіус: <b>{radius:g} км</b>\nБС: <code>{point['lat']:.7f}, {point['lon']:.7f}</code>"
    try:
        image = await map_preview(point["lat"], point["lon"], azimuth, radius)
        await update.effective_message.reply_photo(image, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        log.exception("Preview generation failed")
        await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await reset_state(update, context)
    await update.effective_message.reply_text("Готово. Оберіть наступну дію:", reply_markup=main_keyboard(update.effective_user.id))
    return MENU


async def users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return MENU
    with db() as connection:
        rows = connection.execute("SELECT * FROM users ORDER BY updated_at DESC").fetchall()
    for row in rows:
        if row["user_id"] in settings().admin_ids:
            continue
        action = "revoke" if row["status"] == "approved" else "restore"
        label = "⛔ Скасувати доступ" if action == "revoke" else "✅ Відновити доступ"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"manage:{action}:{row['user_id']}")]])
        await update.effective_message.reply_text(
            f"<b>{row['first_name'] or 'Без імені'} {row['last_name'] or ''}</b>\nТелефон: <code>{row['phone'] or 'не надано'}</code>\nID: <code>{row['user_id']}</code>\nСтатус: <b>{row['status']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    return MENU


async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    _, action, user_id_text = query.data.split(":", 2)
    user_id = int(user_id_text)
    status = "blocked" if action == "revoke" else "approved"
    set_status(user_id, status)
    opposite = "restore" if status == "blocked" else "revoke"
    label = "✅ Відновити доступ" if opposite == "restore" else "⛔ Скасувати доступ"
    await query.edit_message_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"manage:{opposite}:{user_id}")]]))
    try:
        await context.bot.send_message(user_id, "⛔ Доступ скасовано." if status == "blocked" else "✅ Доступ відновлено. Натисніть /start.")
    except Exception:
        pass


def build_bot() -> Application:
    app = ApplicationBuilder().token(settings().token).concurrent_updates(False).build()
    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.Regex(f"^{re.escape(BTN_RESTART)}$"), start)],
        states={
            ACCESS: [MessageHandler(filters.CONTACT, receive_contact), CommandHandler("start", start)],
            MENU: [
                MessageHandler(filters.Regex(f"^{re.escape(BTN_NEW)}$"), show_menu),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_LOCATION)}$"), choose_location),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_ADDRESS)}$"), choose_address),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_RESTART)}$"), start),
                MessageHandler(filters.Regex(f"^{re.escape(BTN_USERS)}$"), users_menu),
            ],
            WAIT_LOCATION: [MessageHandler(filters.LOCATION, receive_location), MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), show_menu)],
            WAIT_ADDRESS: [MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), show_menu), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
            WAIT_CHOICE: [CallbackQueryHandler(choose_candidate, pattern=r"^point:\d+$"), MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), show_menu)],
            WAIT_AZIMUTH: [MessageHandler(filters.Regex(f"^{re.escape(BTN_CANCEL)}$"), show_menu), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_azimuth)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", show_menu)],
        allow_reentry=True,
    )
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(access_callback, pattern=r"^access:"))
    app.add_handler(CallbackQueryHandler(manage_callback, pattern=r"^manage:"))
    return app


templates = Jinja2Templates(directory="templates")

@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_settings()
    init_db()
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
    return {"status": "ok", "service": "Duga Telegram Bot v2"}

@api.head("/")
async def root_head():
    return None

@api.get("/health")
async def health():
    return {"status": "ok"}

@api.get("/map/{token}", response_class=HTMLResponse)
async def map_page(request: Request, token: str):
    try:
        data = URLSafeSerializer(settings().secret, salt="sector-v2").loads(token)
    except BadSignature as exc:
        raise HTTPException(400, "Недійсне посилання") from exc
    return templates.TemplateResponse("map.html", {
        "request": request,
        "lat": float(data["lat"]),
        "lon": float(data["lon"]),
        "azimuth": float(data["az"]),
        "radius_m": float(data["radius"]) * 1000,
    })
