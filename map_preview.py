from __future__ import annotations

import asyncio
import math
from io import BytesIO

import httpx
from PIL import Image, ImageDraw

TILE_SIZE = 256
EARTH_RADIUS_M = 6371008.8


def _zoom_for_radius(radius_km: float) -> int:
    if radius_km <= 1:
        return 15
    if radius_km <= 2:
        return 14
    if radius_km <= 4:
        return 13
    if radius_km <= 8:
        return 12
    if radius_km <= 16:
        return 11
    if radius_km <= 32:
        return 10
    if radius_km <= 64:
        return 9
    return 8


def _latlon_to_world(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    scale = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


def _destination(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    theta = math.radians(bearing_deg % 360)
    delta = distance_m / EARTH_RADIUS_M
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta)
        + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), ((math.degrees(lambda2) + 540) % 360) - 180


async def _download_tile(client: httpx.AsyncClient, zoom: int, x: int, y: int) -> tuple[int, int, Image.Image | None]:
    maximum = 2 ** zoom
    wrapped_x = x % maximum
    if y < 0 or y >= maximum:
        return x, y, None
    url = f"https://tile.openstreetmap.org/{zoom}/{wrapped_x}/{y}.png"
    try:
        response = await client.get(url)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content)).convert("RGB")
        return x, y, image
    except Exception:
        return x, y, None


async def create_map_preview(
    lat: float,
    lon: float,
    azimuth: float,
    radius_km: float,
    user_agent: str,
    width: int = 900,
    height: int = 600,
) -> BytesIO:
    zoom = _zoom_for_radius(radius_km)
    center_x, center_y = _latlon_to_world(lat, lon, zoom)
    left = center_x - width / 2
    top = center_y - height / 2

    min_tile_x = math.floor(left / TILE_SIZE)
    max_tile_x = math.floor((left + width) / TILE_SIZE)
    min_tile_y = math.floor(top / TILE_SIZE)
    max_tile_y = math.floor((top + height) / TILE_SIZE)

    canvas = Image.new("RGB", (width, height), (235, 235, 235))
    headers = {"User-Agent": user_agent}
    async with httpx.AsyncClient(timeout=12, headers=headers) as client:
        tasks = [
            _download_tile(client, zoom, x, y)
            for x in range(min_tile_x, max_tile_x + 1)
            for y in range(min_tile_y, max_tile_y + 1)
        ]
        for tile_x, tile_y, tile in await asyncio.gather(*tasks):
            if tile is None:
                continue
            paste_x = round(tile_x * TILE_SIZE - left)
            paste_y = round(tile_y * TILE_SIZE - top)
            canvas.paste(tile, (paste_x, paste_y))

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    center = (width / 2, height / 2)

    def point_for(bearing: float, distance_m: float) -> tuple[float, float]:
        p_lat, p_lon = _destination(lat, lon, bearing, distance_m)
        world_x, world_y = _latlon_to_world(p_lat, p_lon, zoom)
        return world_x - left, world_y - top

    # Smooth radial fade using concentric sector polygons.
    steps = 24
    for index in range(steps, 0, -1):
        fraction = index / steps
        distance = radius_km * 1000 * fraction
        alpha = round(8 + 72 * (1 - fraction) ** 0.65)
        points = [center]
        for offset in range(-60, 61, 2):
            points.append(point_for(azimuth + offset, distance))
        ImageDraw.Draw(overlay).polygon(points, fill=(220, 38, 38, alpha))

    draw = ImageDraw.Draw(overlay)
    left_edge = point_for(azimuth - 60, radius_km * 1000)
    right_edge = point_for(azimuth + 60, radius_km * 1000)
    arc_points = [point_for(azimuth + offset, radius_km * 1000) for offset in range(-60, 61, 2)]

    draw.line([center, left_edge], fill=(185, 28, 28, 255), width=5)
    draw.line([center, right_edge], fill=(185, 28, 28, 255), width=5)
    draw.line(arc_points, fill=(220, 38, 38, 210), width=4)

    cx, cy = center
    draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=(239, 68, 68, 255), outline=(127, 29, 29, 255), width=4)
    draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=(255, 255, 255, 255))

    # Attribution strip required for OpenStreetMap tiles.
    draw.rectangle((0, height - 28, width, height), fill=(255, 255, 255, 215))
    draw.text((10, height - 22), "© OpenStreetMap contributors", fill=(30, 30, 30, 255))

    result = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    output = BytesIO()
    result.save(output, format="JPEG", quality=90, optimize=True)
    output.seek(0)
    output.name = "sector-map.jpg"
    return output
