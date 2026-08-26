import { neon } from '@neondatabase/serverless';
import APP_HTML_RAW from './app.html';

let schemaPromise = null;

function adminIds(env) {
  return new Set(String(env.ADMIN_TELEGRAM_USER_IDS || '').split(',').map(x => Number(x.trim())).filter(Number.isFinite));
}

function isAdmin(env, userId) {
  return adminIds(env).has(Number(userId));
}

function sqlClient(env) {
  if (!env.DATABASE_URL) throw new Error('DATABASE_URL is missing');
  return neon(env.DATABASE_URL);
}

async function ensureSchema(env) {
  if (!schemaPromise) {
    schemaPromise = (async () => {
      const sql = sqlClient(env);
      await sql`CREATE TABLE IF NOT EXISTS users(
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        phone TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','blocked')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )`;
      await sql`CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)`;
      await sql`CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC)`;
      await sql`CREATE TABLE IF NOT EXISTS admin_state(
        user_id BIGINT PRIMARY KEY,
        awaiting_broadcast BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )`;
      await sql`CREATE TABLE IF NOT EXISTS temporary_bot_messages(
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(chat_id,message_id)
      )`;
      for (const id of adminIds(env)) {
        await sql`INSERT INTO users(user_id,status,created_at,updated_at)
          VALUES(${id},'approved',NOW(),NOW())
          ON CONFLICT(user_id) DO UPDATE SET status='approved',updated_at=NOW()`;
      }
    })().catch(err => {
      schemaPromise = null;
      throw err;
    });
  }
  return schemaPromise;
}

async function getUser(env, userId) {
  await ensureSchema(env);
  const sql = sqlClient(env);
  const rows = await sql`SELECT * FROM users WHERE user_id=${Number(userId)} LIMIT 1`;
  return rows[0] || null;
}

async function upsertUser(env, user, phone = null) {
  await ensureSchema(env);
  const sql = sqlClient(env);
  const id = Number(user.id);
  const username = user.username || null;
  const firstName = user.first_name || null;
  const lastName = user.last_name || null;
  const initialStatus = isAdmin(env, id) ? 'approved' : 'pending';
  await sql`INSERT INTO users(user_id,username,first_name,last_name,phone,status,created_at,updated_at)
    VALUES(${id},${username},${firstName},${lastName},${phone},${initialStatus},NOW(),NOW())
    ON CONFLICT(user_id) DO UPDATE SET
      username=EXCLUDED.username,
      first_name=EXCLUDED.first_name,
      last_name=EXCLUDED.last_name,
      phone=COALESCE(EXCLUDED.phone,users.phone),
      updated_at=NOW()`;
  if (isAdmin(env, id)) {
    await sql`UPDATE users SET status='approved',updated_at=NOW() WHERE user_id=${id}`;
  }
  return getUser(env, id);
}

async function setStatus(env, userId, status) {
  const sql = sqlClient(env);
  await sql`UPDATE users SET status=${status},updated_at=NOW() WHERE user_id=${Number(userId)}`;
}

async function setPhone(env, userId, phone) {
  const sql = sqlClient(env);
  await sql`UPDATE users SET phone=${phone},updated_at=NOW() WHERE user_id=${Number(userId)}`;
}

async function hasAccess(env, userId) {
  const row = await getUser(env, userId);
  return Boolean(row && row.phone && (row.status === 'approved' || isAdmin(env, userId)));
}

function baseUrl(env, request) {
  return String(env.PUBLIC_BASE_URL || new URL(request.url).origin).replace(/\/$/, '');
}

async function tg(env, method, payload = {}, options = {}) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN is missing');
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!data.ok) throw new Error(`Telegram ${method}: ${data.description || r.status}`);
  if (method === 'sendMessage' && !options.preserve) {
    await rememberTemporaryBotMessage(env, payload.chat_id, data.result?.message_id);
  }
  return data.result;
}

function cleanupError(error) {
  return error instanceof Error ? error.message : String(error);
}

async function rememberTemporaryBotMessage(env, chatId, messageId) {
  const numericChatId = Number(chatId);
  const numericMessageId = Number(messageId);
  if (!Number.isSafeInteger(numericChatId) || !Number.isSafeInteger(numericMessageId)) return;
  try {
    await ensureSchema(env);
    const sql = sqlClient(env);
    await sql`INSERT INTO temporary_bot_messages(chat_id,message_id,created_at)
      VALUES(${numericChatId},${numericMessageId},NOW())
      ON CONFLICT(chat_id,message_id) DO NOTHING`;
  } catch (error) {
    console.warn(JSON.stringify({
      message: 'Failed to remember temporary Telegram message',
      chat_id: numericChatId,
      message_id: numericMessageId,
      error: cleanupError(error),
    }));
  }
}

async function safeDeleteMessage(env, chatId, messageId) {
  if (!Number.isSafeInteger(Number(chatId)) || !Number.isSafeInteger(Number(messageId))) return false;
  try {
    await tg(env, 'deleteMessage', { chat_id: chatId, message_id: messageId }, { preserve: true });
    return true;
  } catch (error) {
    console.warn(JSON.stringify({
      message: 'Telegram message cleanup skipped',
      chat_id: Number(chatId),
      message_id: Number(messageId),
      error: cleanupError(error),
    }));
    return false;
  }
}

async function cleanupTemporaryBotMessages(env, chatId) {
  const numericChatId = Number(chatId);
  if (!Number.isSafeInteger(numericChatId)) return;
  let rows;
  try {
    await ensureSchema(env);
    const sql = sqlClient(env);
    rows = await sql`DELETE FROM temporary_bot_messages
      WHERE chat_id=${numericChatId}
      RETURNING message_id`;
  } catch (error) {
    console.warn(JSON.stringify({
      message: 'Failed to load temporary Telegram messages for cleanup',
      chat_id: numericChatId,
      error: cleanupError(error),
    }));
    return;
  }
  await Promise.all(rows.map(row => safeDeleteMessage(env, numericChatId, Number(row.message_id))));
}

async function tgDocument(env, chatId, filename, text, caption = '') {
  const form = new FormData();
  form.append('chat_id', String(chatId));
  form.append('caption', caption);
  form.append('document', new Blob([text], { type: 'text/csv;charset=utf-8' }), filename);
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`, { method: 'POST', body: form });
  const data = await r.json();
  if (!data.ok) throw new Error(data.description || 'sendDocument failed');
  return data.result;
}

const START_BUTTON = 'START';
const BACK_BUTTON = '⬅️ Назад';

function navigationKeyboard(extraRows = []) {
  return {
    keyboard: [
      ...extraRows,
      [{ text: START_BUTTON }],
      [{ text: BACK_BUTTON }],
    ],
    resize_keyboard: true,
    is_persistent: true,
  };
}

function contactKeyboard() {
  return navigationKeyboard([
    [{ text: '📱 Надіслати свій контакт', request_contact: true }],
  ]);
}

function mainKeyboard(env, userId, url) {
  const rows = [
    [{ text: '🚀 Запустити DUGA', web_app: { url: `${url}/app` } }],
  ];
  if (isAdmin(env, userId)) {
    rows.push([{ text: '👥 Користувачі', callback_data: 'users:categories' }]);
    rows.push([{ text: '📢 Повідомлення користувачам', callback_data: 'main:broadcast' }]);
  }
  rows.push([{ text: '🔄 Перезапустити бота', callback_data: 'main:restart' }]);
  rows.push([{ text: BACK_BUTTON, callback_data: 'main:back' }]);
  rows.push([{ text: '❌ Скасувати', callback_data: 'main:cancel' }]);
  return { inline_keyboard: rows };
}

async function sendMain(env, chatId, userId, url, text = 'Оберіть дію:', options = {}) {
  await tg(env, 'sendMessage', { chat_id: chatId, text, reply_markup: navigationKeyboard() }, { preserve: options.preserveText });
  return tg(env, 'sendMessage', {
    chat_id: chatId,
    text: 'Меню DUGA:',
    reply_markup: mainKeyboard(env, userId, url),
  });
}

async function sendWelcome(env, chatId) {
  return tg(env, 'sendMessage', {
    chat_id: chatId,
    text: '🚀 DUGA готова до роботи.',
    reply_markup: navigationKeyboard(),
  });
}

function htmlEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

async function notifyAdminsPending(env, row) {
  const name = [row.first_name, row.last_name].filter(Boolean).join(' ') || 'Без імені';
  const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : 'не вказано';
  const text = `⏳ <b>Новий користувач очікує дозволу</b>\n\n<b>${htmlEscape(name)}</b>\nUsername: <code>${htmlEscape(username)}</code>\nТелефон: <code>${htmlEscape(row.phone || 'не надано')}</code>\nTelegram ID: <code>${row.user_id}</code>`;
  const reply_markup = { inline_keyboard: [
    [
      { text: '✅ Надати доступ', callback_data: `manage:restore:${row.user_id}` },
      { text: '⛔ Заблокувати', callback_data: `manage:revoke:${row.user_id}` },
    ],
    [{ text: BACK_BUTTON, callback_data: 'users:categories' }],
  ] };
  for (const adminId of adminIds(env)) {
    try { await tg(env, 'sendMessage', { chat_id: adminId, text, parse_mode: 'HTML', reply_markup }, { preserve: true }); } catch (_) {}
  }
}

async function usersCounts(env) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT
    COUNT(*) FILTER (WHERE status='pending' AND COALESCE(phone,'')<>'')::int AS pending,
    COUNT(*) FILTER (WHERE status='approved')::int AS approved,
    COUNT(*) FILTER (WHERE status='blocked')::int AS blocked
    FROM users`;
  return rows[0] || { pending: 0, approved: 0, blocked: 0 };
}

async function showUsersMenu(env, chatId) {
  const c = await usersCounts(env);
  return tg(env, 'sendMessage', {
    chat_id: chatId,
    text: '👥 <b>Керування користувачами</b>\n\nОберіть категорію:',
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [
      [{ text: `⏳ Потребують дозволу · ${c.pending}`, callback_data: 'users:list:pending' }],
      [{ text: `✅ Надано доступ · ${c.approved}`, callback_data: 'users:list:approved' }],
      [{ text: `⛔ Заблоковані · ${c.blocked}`, callback_data: 'users:list:blocked' }],
      [{ text: '📋 Завантажити список користувачів', callback_data: 'users:export' }],
      [{ text: BACK_BUTTON, callback_data: 'main:menu' }],
    ] },
  });
}

async function showCategory(env, chatId, category) {
  const sql = sqlClient(env);
  let rows;
  if (category === 'pending') rows = await sql`SELECT * FROM users WHERE status='pending' AND COALESCE(phone,'')<>'' ORDER BY updated_at DESC LIMIT 100`;
  else rows = await sql`SELECT * FROM users WHERE status=${category} ORDER BY updated_at DESC LIMIT 100`;
  const label = category === 'pending' ? '⏳ Потребують дозволу' : category === 'approved' ? '✅ Користувачі з доступом' : '⛔ Заблоковані користувачі';
  await tg(env, 'sendMessage', {
    chat_id: chatId,
    text: `${label}\nКількість: ${rows.length}`,
    reply_markup: { inline_keyboard: [[{ text: BACK_BUTTON, callback_data: 'users:categories' }]] },
  });
  if (!rows.length) return;
  for (const row of rows) {
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ') || 'Без імені';
    const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : 'не вказано';
    const role = isAdmin(env, row.user_id) ? '🛡 Адміністратор' : category === 'approved' ? '✅ Дозволено' : category === 'pending' ? '⏳ Очікує дозволу' : '⛔ Заблоковано';
    const buttons = [];
    if (row.username) buttons.push([{ text: '👤 Відкрити профіль', url: `https://t.me/${String(row.username).replace(/^@/, '')}` }]);
    if (!isAdmin(env, row.user_id)) {
      if (category === 'pending') buttons.push([{ text: '✅ Надати доступ', callback_data: `manage:restore:${row.user_id}` }, { text: '⛔ Заблокувати', callback_data: `manage:revoke:${row.user_id}` }]);
      if (category === 'approved') buttons.push([{ text: '⛔ Скасувати доступ', callback_data: `manage:revoke:${row.user_id}` }]);
      if (category === 'blocked') buttons.push([{ text: '✅ Відновити доступ', callback_data: `manage:restore:${row.user_id}` }]);
    }
    buttons.push([{ text: BACK_BUTTON, callback_data: 'users:categories' }]);
    await tg(env, 'sendMessage', {
      chat_id: chatId,
      text: `<b>${htmlEscape(name)}</b>\nUsername: <code>${htmlEscape(username)}</code>\nТелефон: <code>${htmlEscape(row.phone || 'не надано')}</code>\nTelegram ID: <code>${row.user_id}</code>\nСтатус: <b>${htmlEscape(role)}</b>`,
      parse_mode: 'HTML',
      reply_markup: { inline_keyboard: buttons },
      disable_web_page_preview: true,
    });
  }
}

async function exportUsers(env, chatId) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT * FROM users ORDER BY status,updated_at DESC,user_id DESC`;
  const quoteCsv = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const lines = [`${quoteCsv("Ім'я")};${quoteCsv('Username')};${quoteCsv('Телефон')};${quoteCsv('Telegram ID')};${quoteCsv('Статус')};${quoteCsv('Створено')};${quoteCsv('Оновлено')}`];
  for (const row of rows) {
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ') || 'Без імені';
    const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : '';
    const status = row.status === 'approved' ? '✅ Дозволено' : row.status === 'pending' ? '⏳ Очікує дозволу' : '⛔ Заблоковано';
    lines.push([name, username, row.phone || '', row.user_id, status, row.created_at, row.updated_at].map(quoteCsv).join(';'));
  }
  const filename = `DUGA_users_${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}.csv`;
  await tgDocument(env, chatId, filename, '\uFEFF' + lines.join('\n'), `📋 Список користувачів DUGA\nКількість: ${rows.length}`);
}

async function setBroadcastState(env, userId, value) {
  const sql = sqlClient(env);
  await sql`INSERT INTO admin_state(user_id,awaiting_broadcast,updated_at)
    VALUES(${Number(userId)},${Boolean(value)},NOW())
    ON CONFLICT(user_id) DO UPDATE SET awaiting_broadcast=EXCLUDED.awaiting_broadcast,updated_at=NOW()`;
}

async function getBroadcastState(env, userId) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT awaiting_broadcast FROM admin_state WHERE user_id=${Number(userId)}`;
  return Boolean(rows[0]?.awaiting_broadcast);
}

async function runBroadcast(env, adminChatId, text, url) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT user_id FROM users ORDER BY user_id`;
  let delivered = 0, failed = 0;
  for (const row of rows) {
    try {
      await tg(env, 'sendMessage', { chat_id: Number(row.user_id), text: `📢 Повідомлення адміністратора\n\n${text}` }, { preserve: true });
      delivered++;
    } catch (_) { failed++; }
  }
  await sendMain(env, adminChatId, adminChatId, url, `✅ Розсилку завершено.\nДоставлено: ${delivered}\nНе доставлено: ${failed}`, { preserveText: true });
}

async function handleCallback(env, query, url) {
  const user = query.from;
  const chatId = query.message?.chat?.id || user.id;
  const data = query.data || '';
  if (data === 'start_bot' || data === 'main:menu') {
    await tg(env, 'answerCallbackQuery', { callback_query_id: query.id });
    return sendMain(env, chatId, user.id, url);
  }
  if (data === 'main:back') {
    await tg(env, 'answerCallbackQuery', { callback_query_id: query.id });
    if (isAdmin(env, user.id)) await setBroadcastState(env, user.id, false);
    return sendWelcome(env, chatId);
  }
  if (data === 'main:restart' || data === 'main:cancel') {
    await tg(env, 'answerCallbackQuery', { callback_query_id: query.id });
    if (data === 'main:cancel' && isAdmin(env, user.id)) await setBroadcastState(env, user.id, false);
    return sendMain(env, chatId, user.id, url, data === 'main:cancel' ? 'Дію скасовано.' : 'Бота перезапущено.');
  }
  if (!isAdmin(env, user.id)) {
    await tg(env, 'answerCallbackQuery', { callback_query_id: query.id, text: 'Недостатньо прав', show_alert: true });
    return;
  }
  await tg(env, 'answerCallbackQuery', { callback_query_id: query.id });
  if (data === 'main:broadcast') {
    await setBroadcastState(env, user.id, true);
    return tg(env, 'sendMessage', {
      chat_id: chatId,
      text: '✍️ Надішліть наступним повідомленням текст розсилки.\nДля скасування натисніть кнопку нижче.',
      reply_markup: navigationKeyboard([[{ text: '❌ Скасувати розсилку' }]]),
    });
  }
  if (data === 'users:categories') return showUsersMenu(env, chatId);
  if (data.startsWith('users:list:')) return showCategory(env, chatId, data.split(':').at(-1));
  if (data === 'users:export') return exportUsers(env, chatId);
  if (data.startsWith('manage:restore:')) {
    const id = Number(data.split(':').at(-1));
    await setStatus(env, id, 'approved');
    await tg(env, 'sendMessage', { chat_id: chatId, text: `✅ Доступ користувачу ${id} надано.` }, { preserve: true });
    try { await sendMain(env, id, id, url, '✅ Адміністратор надав вам доступ до DUGA.', { preserveText: true }); } catch (_) {}
    return;
  }
  if (data.startsWith('manage:revoke:')) {
    const id = Number(data.split(':').at(-1));
    await setStatus(env, id, 'blocked');
    await tg(env, 'sendMessage', { chat_id: chatId, text: `⛔ Доступ користувачу ${id} скасовано.` }, { preserve: true });
    try { await tg(env, 'sendMessage', { chat_id: id, text: '⛔ Ваш доступ до DUGA скасовано адміністратором.' }, { preserve: true }); } catch (_) {}
  }
}

async function processTelegramMessage(env, msg, url) {
  const user = msg.from;
  const chatId = msg.chat.id;
  let row = await upsertUser(env, user);

  if (row.status === 'blocked' && !isAdmin(env, user.id)) {
    await tg(env, 'sendMessage', { chat_id: chatId, text: '⛔ Доступ до DUGA скасовано адміністратором.' });
    return;
  }

  if (msg.contact) {
    if (Number(msg.contact.user_id || 0) !== Number(user.id)) {
      await tg(env, 'sendMessage', { chat_id: chatId, text: '⚠️ Потрібно надіслати саме свій номер телефону.', reply_markup: contactKeyboard() });
      return;
    }
    await setPhone(env, user.id, msg.contact.phone_number);
    row = await getUser(env, user.id);
    if (isAdmin(env, user.id) || row.status === 'approved') {
      await sendMain(env, chatId, user.id, url, '✅ Номер телефону збережено.');
    } else {
      await tg(env, 'sendMessage', { chat_id: chatId, text: '✅ Номер телефону збережено.\n⏳ Заявку передано адміністратору. Очікуйте надання доступу.', reply_markup: navigationKeyboard() });
      await notifyAdminsPending(env, row);
    }
    return;
  }

  if (!row.phone) {
    await tg(env, 'sendMessage', {
      chat_id: chatId,
      text: '📱 Для користування DUGA надішліть свій номер телефону кнопкою нижче. Запит повторюватиметься, доки номер не буде збережено.',
      reply_markup: contactKeyboard(),
    });
    return;
  }

  const text = String(msg.text || '').trim();
  if (text === '/start' || text.startsWith('/start ')) {
    if (isAdmin(env, user.id) || row.status === 'approved') await sendWelcome(env, chatId);
    else await tg(env, 'sendMessage', { chat_id: chatId, text: '⏳ Ваш номер уже збережено. Очікуйте дозволу адміністратора.', reply_markup: navigationKeyboard() });
    return;
  }
  if (text.toUpperCase() === START_BUTTON) {
    if (isAdmin(env, user.id) || row.status === 'approved') await sendMain(env, chatId, user.id, url);
    else await tg(env, 'sendMessage', { chat_id: chatId, text: '⏳ Очікуйте дозволу адміністратора.', reply_markup: navigationKeyboard() });
    return;
  }
  if (text === BACK_BUTTON) {
    if (isAdmin(env, user.id)) await setBroadcastState(env, user.id, false);
    if (isAdmin(env, user.id) || row.status === 'approved') await sendMain(env, chatId, user.id, url, 'Повернення до головного меню.');
    else await tg(env, 'sendMessage', { chat_id: chatId, text: '⏳ Очікуйте дозволу адміністратора.', reply_markup: navigationKeyboard() });
    return;
  }
  if (text === '/help') {
    await tg(env, 'sendMessage', { chat_id: chatId, text: '📡 DUGA\n\n1. Запустіть Mini App.\n2. Оберіть до 3 точок.\n3. Для кожної задайте азимут і радіус.\n4. За потреби увімкніть спільний полігон або повноекранний режим.' });
    return;
  }
  if (text === '/status' && isAdmin(env, user.id)) {
    let db = '✅ PostgreSQL: працює';
    try { const sql = sqlClient(env); await sql`SELECT 1`; } catch (_) { db = '❌ PostgreSQL: помилка'; }
    await tg(env, 'sendMessage', { chat_id: chatId, text: `🩺 Стан DUGA\n\n✅ Cloudflare Worker: працює\n${db}\n✅ Telegram: webhook отримано\n🕒 ${new Date().toISOString()}` });
    return;
  }
  if (isAdmin(env, user.id) && (text === '/broadcast' || text === '📢 Повідомлення користувачам')) {
    await setBroadcastState(env, user.id, true);
    await tg(env, 'sendMessage', { chat_id: chatId, text: '✍️ Надішліть наступним повідомленням текст розсилки.\nДля скасування натисніть кнопку нижче.', reply_markup: navigationKeyboard([[{ text: '❌ Скасувати розсилку' }]]) });
    return;
  }
  if (isAdmin(env, user.id) && text === '❌ Скасувати розсилку') {
    await setBroadcastState(env, user.id, false);
    await sendMain(env, chatId, user.id, url, 'Розсилку скасовано.');
    return;
  }
  if (isAdmin(env, user.id) && await getBroadcastState(env, user.id)) {
    await setBroadcastState(env, user.id, false);
    if (text) await runBroadcast(env, chatId, text, url);
    return;
  }
  if (isAdmin(env, user.id) && text === '👥 Користувачі') return showUsersMenu(env, chatId);
  if (text === '🔄 Перезапустити бота' || text === '❌ Скасувати') {
    if (isAdmin(env, user.id) || row.status === 'approved') return sendMain(env, chatId, user.id, url);
    await tg(env, 'sendMessage', { chat_id: chatId, text: '⏳ Очікуйте дозволу адміністратора.' });
    return;
  }
  if (!(isAdmin(env, user.id) || row.status === 'approved')) {
    await tg(env, 'sendMessage', { chat_id: chatId, text: '⏳ Очікуйте дозволу адміністратора.' });
    return;
  }
  await sendMain(env, chatId, user.id, url);
}

async function processTelegramUpdate(env, update, url) {
  if (update.callback_query) {
    const query = update.callback_query;
    const user = query.from;
    const chatId = query.message?.chat?.id || user?.id;
    if (user) await cleanupTemporaryBotMessages(env, chatId);
    return handleCallback(env, query, url);
  }
  const msg = update.message || update.edited_message;
  if (!msg?.from || !msg.chat) return;
  const adminMessage = isAdmin(env, msg.from.id);
  await cleanupTemporaryBotMessages(env, msg.chat.id);
  try {
    return await processTelegramMessage(env, msg, url);
  } finally {
    if (!adminMessage) await safeDeleteMessage(env, msg.chat.id, msg.message_id);
  }
}

function toHex(bytes) {
  return [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function hmac(keyBytes, message) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message));
}

async function verifyTelegramInitData(env, initData) {
  if (!initData || !env.TELEGRAM_BOT_TOKEN) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get('hash');
  if (!hash) return null;
  params.delete('hash');
  const check = [...params.entries()]
    .map(([key, value]) => `${key}=${value}`)
    .sort()
    .join('\n');
  const secret = await hmac(new TextEncoder().encode('WebAppData'), env.TELEGRAM_BOT_TOKEN);
  const signature = await hmac(new Uint8Array(secret), check);
  if (toHex(signature) !== hash.toLowerCase()) return null;
  const authDate = Number(params.get('auth_date') || 0);
  if (!authDate || Math.abs(Date.now()/1000 - authDate) > 86400 * 7) return null;
  try { return JSON.parse(params.get('user') || 'null'); } catch (_) { return null; }
}

async function webAppAccess(env, request) {
  const initData = request.headers.get('X-Telegram-Init-Data') || '';
  if (!initData) return { ok: false, status: 401, detail: 'Надішліть /start боту та відкрийте DUGA новою кнопкою під повідомленням.' };
  const user = await verifyTelegramInitData(env, initData);
  if (!user) return { ok: false, status: 401, detail: 'Telegram authorization failed' };
  const row = await upsertUser(env, user);
  if (!row.phone) return { ok: false, status: 403, detail: 'Спочатку надішліть свій номер телефону боту.' };
  if (!(isAdmin(env, user.id) || row.status === 'approved')) return { ok: false, status: 403, detail: row.status === 'blocked' ? 'Доступ скасовано.' : 'Очікуйте дозволу адміністратора.' };
  return { ok: true, user, row };
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' } });
}

function guardedHtml() {
  const guard = `<script>(function(){const rawFetch=window.fetch.bind(window);window.fetch=(input,init={})=>{try{const u=typeof input==='string'?input:input.url;if(u.startsWith('/api/')){const h=new Headers(init.headers||{});h.set('X-Telegram-Init-Data',window.Telegram?.WebApp?.initData||'');init={...init,headers:h}}}catch(e){}return rawFetch(input,init)};async function guard(){const id='dugaAccessGuard';let box=document.getElementById(id);if(!box){box=document.createElement('div');box.id=id;box.style.cssText='position:fixed;inset:0;z-index:99999;background:#0f1117;color:#fff;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center;font:600 16px system-ui';box.textContent='Перевірка доступу…';document.body.appendChild(box)}try{const r=await fetch('/api/access');const d=await r.json();if(r.ok&&d.ok){box.remove()}else{box.textContent=d.detail||'Доступ відсутній'}}catch(e){box.textContent='Не вдалося перевірити доступ'}}if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',guard);else guard()})();</script>`;
  return APP_HTML_RAW.replace('</body>', `${guard}</body>`);
}

async function geocode(env, request) {
  const access = await webAppAccess(env, request);
  if (!access.ok) return json({ results: [], detail: access.detail }, access.status);
  const q = (new URL(request.url).searchParams.get('q') || '').trim();
  if (q.length < 2) return json({ results: [] });
  const endpoint = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=8&addressdetails=1&q=${encodeURIComponent(q)}`;
  try {
    const r = await fetch(endpoint, { headers: { 'User-Agent': env.NOMINATIM_USER_AGENT || 'DUGA/4.0', 'Accept-Language': 'uk,en;q=0.8' } });
    if (!r.ok) return json({ results: [] });
    const items = await r.json();
    return json({ results: items.slice(0,8).map(x => ({ lat:Number(x.lat), lon:Number(x.lon), label:String(x.display_name || q), source:'OpenStreetMap' })).filter(x => Number.isFinite(x.lat) && Number.isFinite(x.lon)) });
  } catch (_) { return json({ results: [] }); }
}

async function setupWebhook(env, request) {
  const key = new URL(request.url).searchParams.get('key') || '';
  if (!env.SETUP_KEY || key !== env.SETUP_KEY) return json({ ok:false, detail:'Forbidden' }, 403);
  const url = baseUrl(env, request);
  const secret = env.TELEGRAM_WEBHOOK_SECRET || undefined;
  const result = await tg(env, 'setWebhook', {
    url: `${url}/telegram-webhook`,
    allowed_updates: ['message','edited_message','callback_query'],
    drop_pending_updates: false,
    max_connections: 20,
    ...(secret ? { secret_token: secret } : {}),
  });
  return json({ ok:true, webhook:`${url}/telegram-webhook`, result });
}

export default {
  async fetch(request, env) {
    const u = new URL(request.url);
    const path = u.pathname;
    try {
      if (path === '/' || path === '/app') return new Response(guardedHtml(), { headers: { 'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'no-store' } });
      if (path === '/live') return json({ status:'ok', service:'DUGA', runtime:'cloudflare-workers' });
      if (path === '/ready' || path === '/health') {
        await ensureSchema(env);
        const sql = sqlClient(env); await sql`SELECT 1`;
        return json({ status:'ok', service:'DUGA', database:'ok', runtime:'cloudflare-workers' });
      }
      if (path === '/api/access') {
        const access = await webAppAccess(env, request);
        return json(access.ok ? { ok:true } : { ok:false, detail:access.detail }, access.ok ? 200 : access.status);
      }
      if (path === '/api/geocode') return geocode(env, request);
      if (path === '/admin/setup-webhook') return setupWebhook(env, request);
      if (path === '/telegram-webhook' && request.method === 'POST') {
        if (env.TELEGRAM_WEBHOOK_SECRET && request.headers.get('X-Telegram-Bot-Api-Secret-Token') !== env.TELEGRAM_WEBHOOK_SECRET) return json({ ok:false }, 403);
        const update = await request.json();
        await processTelegramUpdate(env, update, baseUrl(env, request));
        return json({ ok:true });
      }
      return json({ detail:'Not found' }, 404);
    } catch (err) {
      console.error('DUGA worker error', err?.stack || err);
      return json({ status:'error', detail:'Temporary service error' }, 500);
    }
  },
};
