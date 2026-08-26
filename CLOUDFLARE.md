# DUGA on Cloudflare Workers

Гілка `cloudflare-worker` містить повний backend DUGA для Cloudflare Workers і не змінює поточну production-гілку `main` до моменту фактичного перемикання Telegram webhook.

## Що вже перенесено

- Telegram webhook без long polling.
- Існуюча PostgreSQL/Neon база через `DATABASE_URL` — користувачі, телефони та статуси залишаються в тій самій базі, тому окрема міграція даних не потрібна.
- Обов'язкове надсилання власного номера телефону.
- Статуси `pending`, `approved`, `blocked`.
- Адмін-категорії користувачів, схвалення, блокування та відновлення.
- CSV-експорт користувачів.
- Асинхронна розсилка всім зареєстрованим користувачам через Cloudflare Queue.
- `/help`, `/status`.
- Mini App: до 3 секторів, окремі азимути, радіуси 1/3/5/10 км, fullscreen, спільний полігон, пошук адрес.
- Перевірка Telegram Mini App `initData` і прав доступу до API.
- Серверна видача коду Mini App лише після успішної перевірки `initData`.
- Безпечне автоочищення службових повідомлень у приватних чатах без видалення повідомлень адміністраторів.
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

Перед першим deploy цієї версії один раз створіть основну чергу та dead-letter queue:

```text
npx wrangler queues create duga-broadcasts
npx wrangler queues create duga-broadcasts-dlq
```

Назви мають збігатися з `wrangler.jsonc`. Сам Worker є одночасно producer і consumer; повідомлення розсилки доставляються невеликими пакетами з контрольованими повторними спробами.

## Обов'язкові Variables and Secrets

Worker → Settings → Variables and Secrets:

- `TELEGRAM_BOT_TOKEN` — актуальний токен BotFather, тип Secret.
- `DATABASE_URL` — той самий PostgreSQL/Neon connection string, який використовує поточний DUGA, тип Secret.
- `ADMIN_TELEGRAM_USER_IDS` — Telegram ID адміністратора або кілька ID через кому.
- `PUBLIC_BASE_URL` — HTTPS URL нового Cloudflare Worker без `/` в кінці.
- `TELEGRAM_WEBHOOK_SECRET` — довгий випадковий секрет для перевірки Telegram webhook, тип Secret.
- `SETUP_KEY` — окремий довгий одноразовий секрет для першого перемикання webhook, тип Secret.
- `NOMINATIM_USER_AGENT` — обов'язковий ідентифікатор із реальним контактом, наприклад `DUGA/4.0 (contact: admin@example.com)`.

`MAP_SECRET`, Uvicorn, Docker і локальна SQLite база для Cloudflare-версії не потрібні.

## Перемикання Telegram webhook

Після успішного deploy та додавання всіх secrets виконайте захищений POST-запит. Він застосує ідемпотентну міграцію схеми БД і лише після цього налаштує Telegram webhook:

```powershell
$headers = @{ Authorization = "Bearer $env:DUGA_SETUP_KEY" }
Invoke-RestMethod -Method Post -Uri "https://<worker-domain>/admin/setup-webhook" -Headers $headers
```

У відповіді має бути:

```json
{"ok":true}
```

Після цього Telegram працюватиме напряму через Cloudflare Workers. Render більше не потрібен як backend.

`SETUP_KEY` більше не передається в URL і не потрапляє до URL-логів. Після успішного перемикання рекомендовано змінити або видалити цей secret.

## Перевірка

- `https://<worker-domain>/live`
- `https://<worker-domain>/ready`
- `npm ci`
- `npm run check`
- `npm test`
- `npx wrangler deploy --dry-run`
- Telegram: `/start`
- Telegram admin: `/status`
- Mini App: запуск, пошук адреси, 3 точки, fullscreen і спільний полігон.

## Безпека

Не додавайте `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `TELEGRAM_WEBHOOK_SECRET` або `SETUP_KEY` у Git. Вони мають зберігатися тільки як Cloudflare Secrets.

Webhook працює за принципом fail-closed: якщо `TELEGRAM_WEBHOOK_SECRET` відсутній або заголовок Telegram неправильний, update не обробляється. Автоочищення виконується лише у приватних чатах, лише після успішної обробки вхідного повідомлення і не зачіпає повідомлення адміністратора. Невдалі видалення не ламають webhook і залишаються в черзі на наступну спробу.

Telegram не дає боту перелічити всю історію чату, тому повідомлення, створені до ввімкнення їх обліку, автоматично знайти неможливо. Також Bot API не дозволяє видаляти більшість повідомлень старше 48 годин; прострочені записи очищаються тільки з технічної таблиці.

Nominatim викликається не частіше одного разу на секунду для всього застосунку, а успішні результати кешуються на 24 години. Відповіді пошуку вставляються в DOM як текст, а не HTML.

<!-- Cloudflare production build trigger after secret rotation: 2026-08-25 -->
