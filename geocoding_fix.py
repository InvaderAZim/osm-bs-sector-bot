from __future__ import annotations

import asyncio
import re

import httpx

import app as base


CITY_ALIASES = {
    "житомир": "Житомир, Житомирська область, Україна",
    "коростень": "Коростень, Житомирська область, Україна",
}

STREET_WORDS = (
    "вул.", "вулиця", "просп.", "проспект", "пров.", "провулок",
    "бульвар", "шосе", "площа", "майдан", "набережна",
)


def address_variants(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text.strip(" ,"))
    variants = [cleaned]

    lower = cleaned.lower()
    city = next((name for name in CITY_ALIASES if lower.startswith(name)), None)
    if city:
        tail = cleaned[len(city):].strip(" ,")
        city_full = CITY_ALIASES[city]
        if tail:
            variants.append(f"{tail}, {city_full}")
            if not any(word in tail.lower() for word in STREET_WORDS):
                variants.append(f"вулиця {tail}, {city_full}")
        variants.append(f"{cleaned}, Житомирська область, Україна")
    else:
        variants.append(f"{cleaned}, Україна")

    # Nominatim frequently resolves Ukrainian streets better with comma-separated house numbers.
    match = re.match(r"^(.+?)\s+(\d+[а-яА-Яa-zA-Z]?)$", cleaned)
    if match:
        variants.append(f"{match.group(1)}, {match.group(2)}, Україна")

    result: list[str] = []
    for item in variants:
        if item and item.casefold() not in {value.casefold() for value in result}:
            result.append(item)
    return result


async def resolve_point(text: str):
    coordinates = base.parse_coords(text)
    if coordinates:
        return coordinates[0], coordinates[1], "Введені координати"

    url = base.extract_url(text)
    if url:
        coordinates = base.coords_from_url(url)
        if coordinates:
            return coordinates[0], coordinates[1], "Точка з картографічного посилання"

    headers = {
        "User-Agent": base.settings().nominatim_user_agent,
        "Accept-Language": "uk,en;q=0.7",
    }
    timeout = httpx.Timeout(8.0, connect=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            for query in address_variants(text):
                params = {
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "ua",
                    "addressdetails": 1,
                    "dedupe": 1,
                }
                try:
                    response = await asyncio.wait_for(
                        client.get("https://nominatim.openstreetmap.org/search", params=params),
                        timeout=9.0,
                    )
                    response.raise_for_status()
                    data = response.json()
                except (asyncio.TimeoutError, httpx.HTTPError, ValueError):
                    continue

                if data:
                    latitude = float(data[0]["lat"])
                    longitude = float(data[0]["lon"])
                    label = str(data[0].get("display_name") or query)[:160]
                    return latitude, longitude, label
    except Exception:
        base.logger.exception("Geocoding failed")

    return None


base.resolve_point = resolve_point
