# DUGA — Telegram Mini App для секторів БС

DUGA будує на OpenStreetMap сектори базових станцій шириною 120° та дозволяє одночасно працювати з трьома точками, окремими азимутами й радіусами, повноекранною картою та спільним полігоном перетину секторів.

## Поточна архітектура

- Telegram Bot API — webhook.
- FastAPI + Uvicorn — API та Telegram Mini App.
- OpenStreetMap + Leaflet — карта.
- PostgreSQL — постійне зберігання користувачів; `DATABASE_URL` обов'язковий у production.
- Render або інший Docker-сумісний always-on web service — хостинг застосунку.

## Змінні середовища

Скопіюйте `.env.example` у `.env` для локального запуску. Реальні токени та секрети ніколи не додавайте в Git.

```env
TELEGRAM_BOT_TOKEN=PASTE_NEW_BOTFATHER_TOKEN_HERE
PUBLIC_BASE_URL=https://your-public-domain.example
MAP_SECRET=replace-with-a-long-random-secret
DATABASE_URL=postgresql://user:password@host/database?sslmode=require
ADMIN_TELEGRAM_USER_IDS=123456789
DEFAULT_RADIUS_KM=15
NOMINATIM_USER_AGENT=DUGA/3.0 (contact: your-email@example.com)
DUGA_COLD_START_NOTICE=false
LOG_LEVEL=INFO
```

## Локальний запуск

```bash
python -m venv .venv
pip install -r requirements.txt
uvicorn entrypoint:api --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t duga .
docker run --env-file .env -p 8000:8000 duga
```

## Production

Для стабільної роботи 24/7 використовуйте web service без sleep/scale-to-zero. Безкоштовний Render Web Service засинає після простою і не підходить для постійної production-роботи.

Рекомендована конфігурація поточного проєкту:

1. Docker web service з одним постійно активним інстансом.
2. `PUBLIC_BASE_URL` — HTTPS-адреса сервісу.
3. PostgreSQL через `DATABASE_URL`.
4. Health check — `/ready`.
5. Liveness endpoint — `/live`.
6. Telegram webhook реєструється автоматично під час старту сервісу.
7. `DUGA_COLD_START_NOTICE=false` для always-on хостингу.

`render.yaml` використовує актуальний формат Render Blueprint, але тариф/instance type слід обирати в акаунті відповідно до потрібної доступності.

## Безпека

- Не публікуйте `TELEGRAM_BOT_TOKEN`, `MAP_SECRET`, `DATABASE_URL` та інші секрети.
- Якщо секрет уже потрапив у Git-історію, простого видалення з поточного README недостатньо — його потрібно замінити/відкликати.
- `.env` уже виключений через `.gitignore`.

## Перевірка стану

- `/live` — процес працює.
- `/ready` — процес працює і PostgreSQL доступний.
- `/health` — сумісний alias для readiness.
- `/status` — діагностика для адміністратора Telegram.
