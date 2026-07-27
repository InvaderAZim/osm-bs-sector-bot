from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import launcher as bot


def query_variants(text: str) -> list[str]:
    value = re.sub(r"\s+", " ", text.strip(" ,"))
    replacements = [
        (r"\bм\.\s*", ""),
        (r"\bмісто\s+", ""),
        (r"\bвул\.\s*", "вулиця "),
        (r"\bул\.\s*", "вулиця "),
        (r"\bбуд\.\s*", ""),
        (r"\bбудинок\s+", ""),
        (r"\bд\.\s*", ""),
        (r"\bпросп\.\s*", "проспект "),
        (r"\bпров\.\s*", "провулок "),
    ]
    normalized = value
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = normalized.strip(" ,")

    variants = [value, normalized]
    if not re.search(r"\bукраїн[аи]\b", normalized, flags=re.IGNORECASE):
        variants.append(f"{normalized}, Україна")

    parts = [part.strip() for part in re.split(r"[,;]", normalized) if part.strip()]
    if len(parts) >= 2:
        variants.append(", ".join(reversed(parts)))

    house_match = re.match(r"^(.+?)\s+(\d+[А-Яа-яA-Za-z]?)$", normalized)
    if house_match:
        variants.append(f"{house_match.group(1)}, {house_match.group(2)}, Україна")

    unique: list[str] = []
    seen: set[str] = set()
    for item in variants:
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for item in items:
        if not any(
            abs(item["lat"] - existing["lat"]) < 0.00008
            and abs(item["lon"] - existing["lon"]) < 0.00008
            for existing in unique
        ):
            unique.append(item)
    return unique[:8]


async def google_search(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    if not bot.settings().google_key:
        return []
    response = await client.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={
            "address": query,
            "key": bot.settings().google_key,
            "language": "uk",
            "region": "ua",
        },
    )
    response.raise_for_status()
    output: list[dict[str, Any]] = []
    for item in response.json().get("results", [])[:5]:
        point = item.get("geometry", {}).get("location", {})
        try:
            output.append({
                "lat": float(point["lat"]),
                "lon": float(point["lng"]),
                "label": str(item.get("formatted_address") or query),
                "source": "Google",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return output


async def nominatim_search(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    response = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "ua",
            "addressdetails": 1,
            "dedupe": 1,
        },
        headers={
            "User-Agent": bot.settings().nominatim_agent,
            "Accept-Language": "uk,en;q=0.7",
        },
    )
    response.raise_for_status()
    return [
        {
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "label": str(item.get("display_name") or query),
            "source": "OpenStreetMap",
        }
        for item in response.json()[:5]
    ]


async def photon_search(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    response = await client.get(
        "https://photon.komoot.io/api/",
        params={"q": query, "limit": 5, "lang": "uk"},
        headers={"User-Agent": bot.settings().nominatim_agent},
    )
    response.raise_for_status()
    output: list[dict[str, Any]] = []
    for feature in response.json().get("features", [])[:5]:
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2:
            continue
        label_parts = [
            properties.get("name"),
            properties.get("street"),
            properties.get("housenumber"),
            properties.get("city") or properties.get("town") or properties.get("village"),
            properties.get("state"),
            properties.get("country"),
        ]
        label = ", ".join(str(x) for x in label_parts if x)
        output.append({
            "lat": float(coordinates[1]),
            "lon": float(coordinates[0]),
            "label": label or query,
            "source": "Photon",
        })
    return output


async def robust_geocode(text: str) -> list[dict[str, Any]]:
    variants = query_variants(text)
    collected: list[dict[str, Any]] = []
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for query in variants[:4]:
            tasks = [
                google_search(client, query),
                nominatim_search(client, query),
                photon_search(client, query),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    collected.extend(result)
            if len(deduplicate(collected)) >= 5:
                break
    return deduplicate(collected)


async def receive_address(update, context):
    text = (update.effective_message.text or "").strip()
    coordinates = bot.parse_coordinates(text)
    if coordinates:
        context.user_data["candidates"] = [{
            "lat": coordinates[0],
            "lon": coordinates[1],
            "label": f"Координати {coordinates[0]:.7f}, {coordinates[1]:.7f}",
            "source": "Координати",
        }]
    else:
        await update.effective_message.reply_text("🔎 Шукаю всі можливі збіги…", reply_markup=bot.cancel_keyboard())
        try:
            context.user_data["candidates"] = await robust_geocode(text)
        except Exception:
            bot.log.exception("Robust address search failed")
            context.user_data["candidates"] = []

    candidates = context.user_data.get("candidates", [])
    if not candidates:
        await update.effective_message.reply_text(
            "Точку не знайдено. Додайте населений пункт, вулицю або номер будинку.",
            reply_markup=bot.cancel_keyboard(),
        )
        return bot.WAIT_ADDRESS

    buttons = []
    for index, item in enumerate(candidates):
        source = item.get("source", "Карта")
        buttons.append([
            InlineKeyboardButton(
                f"{index + 1}. {item['label'][:48]} · {source}",
                callback_data=f"point:{index}",
            )
        ])
    sent = await update.effective_message.reply_text(
        "Оберіть правильну точку зі знайдених варіантів:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    context.user_data.setdefault("inline_messages", []).append(sent.message_id)
    return bot.WAIT_CHOICE


bot.geocode = robust_geocode
bot.receive_address = receive_address
