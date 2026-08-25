# DUGA on Cloudflare Workers

Ця гілка `cloudflare-worker` ізольована від production-гілки `main`, тому поточний Render deployment не ламається.

## Поточний етап

Cloudflare Worker вже містить:

- Telegram Mini App на `/app`;
- до 3 секторів одночасно;
- окремі азимути та радіуси 1/3/5/10 км;
- повноекранний режим із кнопкою виходу внизу;
- режим `Спільний полігон`;
- пошук адрес через OpenStreetMap/Nominatim;
- `/live`, `/ready`, `/health`.

Telegram webhook поки навмисно НЕ переключений на Cloudflare. До завершення перенесення авторизації/бази користувачів бот продовжує працювати через Render.

## Cloudflare Git deployment

На екрані створення Worker:

- Project name: `osm-bs-sector-bot`
- Production branch: `cloudflare-worker`
- Build command: залишити порожнім
- Deploy command: `npx wrangler deploy`

`wrangler.jsonc` використовує Python Workers без зовнішніх Python-пакетів, тому окремий `pywrangler` на цьому етапі не потрібен.

Після першого успішного deploy перевірити:

- `/live`
- `/app`
- пошук адреси
- 3 точки/сектори
- fullscreen
- спільний полігон

## Наступний етап

1. Створити Cloudflare D1 database `duga-users`.
2. Додати D1 binding `DB` у `wrangler.jsonc`.
3. Перенести користувачів/статуси/телефони з PostgreSQL у D1.
4. Перенести Telegram webhook та admin/access flow у Worker.
5. Переключити Telegram webhook на `https://<worker>.workers.dev/telegram-webhook`.
6. Лише після повної перевірки вимкнути Render.
