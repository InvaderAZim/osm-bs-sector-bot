from __future__ import annotations

import asyncio
import re

import httpx
from telegram.constants import ParseMode
from telegram.ext import ConversationHandler

import app as base


CITY_ALIASES = {
    "житомир": "Житомир, Житомирська область, Україна",
    "коростень": "Коростень, Житомирська область, Україна",
}

STREET_PREFIXES = {
    "вул": "вулиця", "вул.": "вулиця", "улица": "вулиця", "ул": "вулиця", "ул.": "вулиця",
    "просп": "проспект", "просп.": "проспект", "пр-т": "проспект",
    "пров": "провулок", "пров.": "провулок", "пер": "провулок", "пер.": "провулок",
}


def normalize_address(text: str) -> str:
    value = text.strip()
    value = re.sub(r"[;|]+", ",", value)
    value = re.sub(r"\b(м\.?|місто)\s+", "", value, flags=re.I)
    value = re.sub(r"\b(буд\.?|будинок|дом|д\.)\s*", "", value, flags=re.I)
    value = re.sub(r"\s*,\s*", ", ", value)
    value = re.sub(r"\s+", " ", value)
    return " ".join(STREET_PREFIXES.get(word.casefold(), word) for word in value.split()).strip(" ,")


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value.strip(" ,"))
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def address_variants(text: str) -> list[str]:
    cleaned = normalize_address(text)
    variants = [cleaned, f"{cleaned}, Україна"]
    lower = cleaned.casefold()
    city = next((name for name in CITY_ALIASES if re.search(rf"\b{re.escape(name)}\b", lower)), None)
    house_match = re.search(r"\b(\d+[а-яa-z]?(?:[/\-]\d+[а-яa-z]?)?)\b", cleaned, flags=re.I)
    house = house_match.group(1) if house_match else None

    if city:
        city_full = CITY_ALIASES[city]
        remainder = re.sub(rf"\b{re.escape(city)}\b", "", cleaned, flags=re.I).strip(" ,")
        variants.extend([
            f"{remainder}, {city_full}",
            f"{city_full}, {remainder}",
            f"{cleaned}, Житомирська область, Україна",
        ])
        if remainder and not re.search(r"\b(вулиця|проспект|провулок|бульвар|шосе|площа|майдан|набережна)\b", remainder, flags=re.I):
            variants.extend([f"вулиця {remainder}, {city_full}", f"{city_full}, вулиця {remainder}"])
        if house:
            street_only = re.sub(rf"\b{re.escape(house)}\b", "", remainder, flags=re.I).strip(" ,")
            variants.extend([
                f"{street_only}, {house}, {city_full}",
                f"{city_full}, {street_only}, {house}",
                f"{house}, {street_only}, {city_full}",
            ])
            if street_only and not re.search(r"\b(вулиця|проспект|провулок|бульвар|шосе|площа|майдан|набережна)\b", street_only, flags=re.I):
                variants.extend([f"вулиця {street_only}, {house}, {city_full}", f"{city_full}, вулиця {street_only}, {house}"])
    elif house:
        street_only = re.sub(rf"\b{re.escape(house)}\b", "", cleaned, flags=re.I).strip(" ,")
        variants.extend([f"{street_only}, {house}, Україна", f"{house}, {street_only}, Україна"])

    variants.append(re.sub(r"\s+(?=\d+[а-яa-z]?(?:[/\-]\d+)?\b)", ", ", cleaned, flags=re.I))
    return unique(variants)


async def nominatim_search(client: httpx.AsyncClient, query: str):
    response = await asyncio.wait_for(
        client.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "ua", "addressdetails": 1, "dedupe": 1},
        ),
        timeout=9.0,
    )
    response.raise_for_status()
    return response.json()


async def photon_search(client: httpx.AsyncClient, query: str):
    response = await asyncio.wait_for(
        client.get("https://photon.komoot.io/api/", params={"q": query, "limit": 1, "lang": "uk"}),
        timeout=9.0,
    )
    response.raise_for_status()
    data = response.json()
    features = data.get("features") or []
    if not features:
        return None
    feature = features[0]
    lon, lat = feature["geometry"]["coordinates"]
    props = feature.get("properties") or {}
    label = ", ".join(str(props.get(key)) for key in ("name", "street", "housenumber", "city", "state", "country") if props.get(key))
    return float(lat), float(lon), label or query


async def resolve_point(text: str):
    coordinates = base.parse_coords(text)
    if coordinates:
        return coordinates[0], coordinates[1], "Введені координати"

    url = base.extract_url(text)
    if url:
        coordinates = base.coords_from_url(url)
        if coordinates:
            return coordinates[0], coordinates[1], "Точка з картографічного посилання"

    headers = {"User-Agent": base.settings().nominatim_user_agent, "Accept-Language": "uk,en;q=0.7"}
    variants = address_variants(text)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0, connect=5.0), headers=headers, follow_redirects=True) as client:
            for query in variants:
                try:
                    data = await nominatim_search(client, query)
                except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
                    continue
                if data:
                    return float(data[0]["lat"]), float(data[0]["lon"]), str(data[0].get("display_name") or query)[:160]

            for query in variants[:5]:
                try:
                    result = await photon_search(client, query)
                except (asyncio.TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
                    continue
                if result:
                    return result[0], result[1], result[2][:160]
    except Exception:
        base.logger.exception("Geocoding failed")

    return None


async def location(update, context):
    if await base.deny(update, context):
        return ConversationHandler.END

    message = update.effective_message
    if message.location:
        lat, lon, label = message.location.latitude, message.location.longitude, "Надіслана геолокація"
    elif message.text:
        result = await resolve_point(message.text)
        if not result:
            await message.reply_text(
                "Точку не знайдено. Спробуйте інший порядок слів, координати, посилання або виберіть точку на карті."
            )
            return base.WAIT_LOCATION
        lat, lon, label = result
    else:
        return base.WAIT_LOCATION

    context.user_data["point"] = {"lat": lat, "lon": lon, "label": label}
    await message.reply_text(
        f"БС: <code>{lat:.7f}, {lon:.7f}</code>\nВведіть азимут, наприклад <code>90</code>, або азимут і радіус: <code>90 15</code>.",
        parse_mode=ParseMode.HTML,
    )
    return base.WAIT_AZIMUTH


base.resolve_point = resolve_point
base.location = location
