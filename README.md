# Telegram-бот «Сектор БС на OpenStreetMap»

Бот приймає геолокацію, координати, адресу або картографічне посилання, запитує азимут і створює інтерактивну карту OpenStreetMap із сектором базової станції шириною 120° — по 60° ліворуч і праворуч від азимуту.

На карті відображаються:

- точка «БС»;
- напівпрозорий червоний сектор;
- плавне згасання кольору до зовнішнього краю;
- дві червоні межові лінії;
- зовнішня дуга;
- заданий радіус сектора.

## Налаштування

Скопіюйте `.env.example` у `.env` і заповніть:

```env
TELEGRAM_BOT_TOKEN=8789151694:AAGIlOMm03GJxPYUaZlY4T5-n3TvpB3sS_Q
PUBLIC_BASE_URL=https://публічна-адреса-сервера
MAP_SECRET=f91c8472d0334a0f978c21e6ab81d9906c57eabc47d831a
DEFAULT_RADIUS_KM=15
ALLOWED_TELEGRAM_USER_IDS=
NOMINATIM_USER_AGENT=OSM-BS-Sector-Telegram-Bot/1.0 (contact: email@example.com)
```

`.env` не потрібно завантажувати в GitHub.

## Локальний запуск

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:api --host 0.0.0.0 --port 8000
```

Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:api --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t osm-bs-sector-bot .
docker run --env-file .env -p 8000:8000 osm-bs-sector-bot
```

## Render

У репозиторії є `render.yaml`. Створіть у Render Blueprint із цього репозиторію, заповніть змінні середовища, а `PUBLIC_BASE_URL` встановіть рівним HTTPS-адресі створеного сервісу.

## Використання

1. Відправте боту `/start`.
2. Надішліть адресу, координати, посилання або геолокацію.
3. Введіть азимут, наприклад `125`.
4. Для іншого радіуса введіть два числа: `125 8`, де `8` — кілометри.

Допустимі межі: азимут `0–359.99°`, радіус `0.1–100 км`.
