# DUGA on Cloudflare Workers

Гілка `cloudflare-worker` містить повний backend DUGA для Cloudflare Workers і не змінює поточну production-гілку `main` до моменту фактичного перемикання Telegram webhook.

## Що вже перенесено

- Telegram webhook без long polling.
- Існуюча PostgreSQL/Neon база через `DATABASE_URL` — користувачі, телефони та статуси залишаються в тій самій базі, тому окрема міграція даних не потрібна.
- Обов'язкове надсилання власного номера телефону.
- Статуси `pending`, `approved`, `blocked`.
- Адмін-категорії користувачів, схвалення, блокування та відновлення.
- CSV-експорт користувачів.
- Розсилка всім зареєстрованим користувачам.
- `/help`, `/status`.
- Mini App: до 3 секторів, окремі азимути, радіуси 1/3/5/10 км, fullscreen, спільний полігон, пошук адрес.
- Перевірка Telegram Mini App `initData` і прав доступу до API.
- `/live`, `/ready`, `/health`.

## Cloudflare Workers Builds

Production branch:

```text
cloudflare-worker
```

Build command: можна залишити порожнім.

Deploy command:

```text
npx wrangler deploy
```

## Обов'язкові Variables and Secrets

Worker → Settings → Variables and Secrets:

- `TELEGRAM_BOT_TOKEN` — актуальний токен BotFather, тип Secret.
- `DATABASE_URL` — той самий PostgreSQL/Neon connection string, який використовує поточний DUGA, тип Secret.
- `ADMIN_TELEGRAM_USER_IDS` — Telegram ID адміністратора або кілька ID через кому.
- `PUBLIC_BASE_URL` — HTTPS URL нового Cloudflare Worker без `/` в кінці.
- `TELEGRAM_WEBHOOK_SECRET` — довгий випадковий секрет для перевірки Telegram webhook, тип Secret.
- `SETUP_KEY` — окремий довгий одноразовий секрет для першого перемикання webhook, тип Secret.
- `NOMINATIM_USER_AGENT` — наприклад `DUGA/4.0 (contact: admin@example.com)`.

`MAP_SECRET`, Uvicorn, Docker і локальна SQLite база для Cloudflare-версії не потрібні.

## Перемикання Telegram webhook

Після успішного deploy та додавання всіх secrets один раз відкрийте:

```text
https://<worker-domain>/admin/setup-webhook?key=<SETUP_KEY>
```

У відповіді має бути:

```json
{"ok":true}
```

Після цього Telegram працюватиме напряму через Cloudflare Workers. Render більше не потрібен як backend.

Після успішного перемикання рекомендовано змінити або видалити `SETUP_KEY`.

## Перевірка

- `https://<worker-domain>/live`
- `https://<worker-domain>/ready`
- Telegram: `/start`
- Telegram admin: `/status`
- Mini App: запуск, пошук адреси, 3 точки, fullscreen і спільний полігон.

## Безпека

Не додавайте `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `TELEGRAM_WEBHOOK_SECRET` або `SETUP_KEY` у Git. Вони мають зберігатися тільки як Cloudflare Secrets.

<!-- Cloudflare production build trigger: 2026-08-25 -->