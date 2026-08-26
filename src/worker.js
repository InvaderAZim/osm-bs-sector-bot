import { neon } from '@neondatabase/serverless';
import APP_HTML_RAW from './app.html';

function adminIds(env) {
  return new Set(String(env.ADMIN_TELEGRAM_USER_IDS || '')
    .split(',')
    .map(value => Number(value.trim()))
    .filter(value => Number.isSafeInteger(value) && value > 0));
}

function isAdmin(env, userId) {
  return adminIds(env).has(Number(userId));
}

function sqlClient(env) {
  if (!env.DATABASE_URL) throw new Error('DATABASE_URL is missing');
  return neon(env.DATABASE_URL);
}

async function migrateSchema(env) {
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
  await sql`CREATE INDEX IF NOT EXISTS idx_temporary_bot_messages_created_at
    ON temporary_bot_messages(created_at)`;
  await sql`CREATE TABLE IF NOT EXISTS admin_notifications(
    user_id BIGINT NOT NULL,
    admin_chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(user_id,admin_chat_id,message_id)
  )`;
  await sql`CREATE INDEX IF NOT EXISTS idx_admin_notifications_active
    ON admin_notifications(user_id,active)`;
  await sql`CREATE TABLE IF NOT EXISTS service_rate_limits(
    service TEXT PRIMARY KEY,
    next_allowed_at TIMESTAMPTZ NOT NULL
  )`;
  await sql`CREATE TABLE IF NOT EXISTS broadcast_jobs(
    job_id UUID PRIMARY KEY,
    admin_chat_id BIGINT NOT NULL,
    public_url TEXT NOT NULL,
    message_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','complete')),
    fanout_complete BOOLEAN NOT NULL DEFAULT FALSE,
    total INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )`;
  await sql`CREATE TABLE IF NOT EXISTS broadcast_deliveries(
    job_id UUID NOT NULL REFERENCES broadcast_jobs(job_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(job_id,user_id)
  )`;
  await sql`CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_status
    ON broadcast_deliveries(job_id,status)`;
  for (const id of adminIds(env)) {
    await sql`INSERT INTO users(user_id,status,created_at,updated_at)
      VALUES(${id},'approved',NOW(),NOW())
      ON CONFLICT(user_id) DO UPDATE SET status='approved',updated_at=NOW()`;
  }
}

async function getUser(env, userId) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT * FROM users WHERE user_id=${Number(userId)} LIMIT 1`;
  return rows[0] || null;
}

async function upsertUser(env, user, phone = null) {
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
    signal: AbortSignal.timeout(15000),
  });
  const data = await r.json().catch(() => null);
  if (!r.ok || !data?.ok) {
    const error = new Error(`Telegram ${method}: ${data?.description || r.status}`);
    error.status = r.status;
    error.telegramCode = Number(data?.error_code || 0);
    error.retryAfter = Number(data?.parameters?.retry_after || 0);
    throw error;
  }
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
  try {
    const sql = sqlClient(env);
    await sql`DELETE FROM temporary_bot_messages
      WHERE chat_id=${numericChatId}
        AND created_at < NOW() - INTERVAL '48 hours'`;
    const rows = await sql`SELECT message_id FROM temporary_bot_messages
      WHERE chat_id=${numericChatId}
        AND created_at >= NOW() - INTERVAL '48 hours'
      ORDER BY message_id
      LIMIT 100`;
    const messageIds = rows.map(row => Number(row.message_id)).filter(Number.isSafeInteger);
    if (!messageIds.length) return;
    if (messageIds.length === 1) {
      await tg(env, 'deleteMessage', {
        chat_id: numericChatId,
        message_id: messageIds[0],
      }, { preserve: true });
    } else {
      await tg(env, 'deleteMessages', {
        chat_id: numericChatId,
        message_ids: messageIds,
      }, { preserve: true });
    }
    await sql`DELETE FROM temporary_bot_messages
      WHERE chat_id=${numericChatId}
        AND message_id = ANY(string_to_array(${messageIds.join(',')}, ',')::bigint[])`;
  } catch (error) {
    console.warn(JSON.stringify({
      message: 'Temporary Telegram messages retained for a later cleanup retry',
      chat_id: numericChatId,
      error: cleanupError(error),
    }));
  }
}

async function tgDocument(env, chatId, filename, text, caption = '') {
  const form = new FormData();
  form.append('chat_id', String(chatId));
  form.append('caption', caption);
  form.append('document', new Blob([text], { type: 'text/csv;charset=utf-8' }), filename);
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`, {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(15000),
  });
  const data = await r.json().catch(() => null);
  if (!r.ok || !data?.ok) throw new Error(data?.description || `sendDocument failed: ${r.status}`);
  return data.result;
}

const START_BUTTON = 'START';
const BACK_BUTTON = '⬅️ Назад';

function navigationKeyboard(extraRows = []) {
  return {
    keyboard: [
      ...extraRows,
      [{ text: START_BUTTON }, { text: BACK_BUTTON }],
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
    try {
      const sent = await tg(env, 'sendMessage', { chat_id: adminId, text, parse_mode: 'HTML', reply_markup }, { preserve: true });
      const sql = sqlClient(env);
      await sql`INSERT INTO admin_notifications(user_id,admin_chat_id,message_id,active,created_at)
        VALUES(${Number(row.user_id)},${Number(adminId)},${Number(sent.message_id)},TRUE,NOW())
        ON CONFLICT(user_id,admin_chat_id,message_id) DO UPDATE SET active=TRUE`;
    } catch (error) {
      console.warn(JSON.stringify({
        message: 'Failed to preserve admin access notification',
        user_id: Number(row.user_id),
        admin_chat_id: Number(adminId),
        error: cleanupError(error),
      }));
    }
  }
}

async function resolveAdminNotifications(env, userId) {
  const sql = sqlClient(env);
  const rows = await sql`SELECT admin_chat_id,message_id FROM admin_notifications
    WHERE user_id=${Number(userId)} AND active=TRUE`;
  for (const row of rows) {
    const chatId = Number(row.admin_chat_id);
    const messageId = Number(row.message_id);
    try {
      await tg(env, 'editMessageReplyMarkup', {
        chat_id: chatId,
        message_id: messageId,
        reply_markup: { inline_keyboard: [[{ text: BACK_BUTTON, callback_data: 'users:categories' }]] },
      }, { preserve: true });
      await sql`UPDATE admin_notifications SET active=FALSE
        WHERE user_id=${Number(userId)} AND admin_chat_id=${chatId} AND message_id=${messageId}`;
    } catch (error) {
      console.warn(JSON.stringify({
        message: 'Failed to retire admin access buttons',
        user_id: Number(userId),
        admin_chat_id: chatId,
        message_id: messageId,
        error: cleanupError(error),
      }));
    }
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

const USERS_PAGE_SIZE = 5;

function userCategory(category) {
  if (category === 'pending') return { label: '⏳ Потребують дозволу', status: 'pending' };
  if (category === 'approved') return { label: '✅ Користувачі з доступом', status: 'approved' };
  if (category === 'blocked') return { label: '⛔ Заблоковані користувачі', status: 'blocked' };
  return null;
}

async function showCategory(env, chatId, category, requestedPage = 0) {
  const config = userCategory(category);
  if (!config) return showUsersMenu(env, chatId);
  const sql = sqlClient(env);
  const countRows = category === 'pending'
    ? await sql`SELECT COUNT(*)::int AS total FROM users WHERE status='pending' AND COALESCE(phone,'')<>''`
    : await sql`SELECT COUNT(*)::int AS total FROM users WHERE status=${config.status}`;
  const total = Number(countRows[0]?.total || 0);
  const lastPage = Math.max(0, Math.ceil(total / USERS_PAGE_SIZE) - 1);
  const page = Math.min(Math.max(0, Number(requestedPage) || 0), lastPage);
  const offset = page * USERS_PAGE_SIZE;
  const rows = category === 'pending'
    ? await sql`SELECT * FROM users WHERE status='pending' AND COALESCE(phone,'')<>'' ORDER BY updated_at DESC,user_id DESC LIMIT ${USERS_PAGE_SIZE} OFFSET ${offset}`
    : await sql`SELECT * FROM users WHERE status=${config.status} ORDER BY updated_at DESC,user_id DESC LIMIT ${USERS_PAGE_SIZE} OFFSET ${offset}`;
  const cards = [];
  const buttons = [];
  for (const row of rows) {
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ') || 'Без імені';
    const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : 'не вказано';
    const role = isAdmin(env, row.user_id) ? '🛡 Адміністратор' : category === 'approved' ? '✅ Дозволено' : category === 'pending' ? '⏳ Очікує дозволу' : '⛔ Заблоковано';
    cards.push(`<b>${htmlEscape(name)}</b>\nUsername: <code>${htmlEscape(username)}</code>\nТелефон: <code>${htmlEscape(row.phone || 'не надано')}</code>\nTelegram ID: <code>${row.user_id}</code>\nСтатус: <b>${htmlEscape(role)}</b>`);
    const shortName = name.length > 20 ? `${name.slice(0, 19)}…` : name;
    const rowButtons = [];
    if (row.username) rowButtons.push({ text: `👤 ${shortName}`, url: `https://t.me/${String(row.username).replace(/^@/, '')}` });
    if (!isAdmin(env, row.user_id)) {
      if (category === 'pending') rowButtons.push({ text: `✅ ${shortName}`, callback_data: `manage:restore:${row.user_id}` }, { text: `⛔ ${shortName}`, callback_data: `manage:revoke:${row.user_id}` });
      if (category === 'approved') rowButtons.push({ text: `⛔ ${shortName}`, callback_data: `manage:revoke:${row.user_id}` });
      if (category === 'blocked') rowButtons.push({ text: `✅ ${shortName}`, callback_data: `manage:restore:${row.user_id}` });
    }
    if (rowButtons.length) buttons.push(rowButtons);
  }
  const pages = [];
  if (page > 0) pages.push({ text: '⬅️', callback_data: `users:list:${category}:${page - 1}` });
  pages.push({ text: `${page + 1}/${lastPage + 1}`, callback_data: 'users:noop' });
  if (page < lastPage) pages.push({ text: '➡️', callback_data: `users:list:${category}:${page + 1}` });
  buttons.push(pages);
  buttons.push([{ text: BACK_BUTTON, callback_data: 'users:categories' }]);
  return tg(env, 'sendMessage', {
    chat_id: chatId,
    text: `<b>${config.label}</b>\nКількість: ${total}\n\n${cards.join('\n\n') || 'Список порожній.'}`,
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: buttons },
    disable_web_page_preview: true,
  });
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

const BROADCAST_FANOUT_SIZE = 50;
const BROADCAST_MAX_ATTEMPTS = 3;

function validBroadcastJobId(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(value || ''));
}

function validTelegramId(value, allowZero = false) {
  const text = String(value || '');
  const numeric = Number(text);
  return (allowZero ? /^\d+$/ : /^[1-9]\d*$/).test(text)
    && Number.isSafeInteger(numeric)
    && (allowZero ? numeric >= 0 : numeric > 0);
}

async function runBroadcast(env, adminChatId, text, url) {
  if (!env.BROADCAST_QUEUE) {
    return sendMain(env, adminChatId, adminChatId, url, '⚠️ Черга розсилки ще не налаштована.', { preserveText: true });
  }
  const sql = sqlClient(env);
  const countRows = await sql`SELECT COUNT(*)::int AS total FROM users`;
  const total = Number(countRows[0]?.total || 0);
  const jobId = crypto.randomUUID();
  const messageText = String(text || '').slice(0, 3900);
  await sql`INSERT INTO broadcast_jobs(job_id,admin_chat_id,public_url,message_text,total,created_at,updated_at)
    VALUES(${jobId}::uuid,${Number(adminChatId)},${url},${messageText},${total},NOW(),NOW())`;
  try {
    await env.BROADCAST_QUEUE.send({ kind: 'fanout', jobId, cursor: '0' });
  } catch (error) {
    await sql`DELETE FROM broadcast_jobs WHERE job_id=${jobId}::uuid`;
    throw error;
  }
  return sendMain(env, adminChatId, adminChatId, url, `📨 Розсилку поставлено в чергу.\nОтримувачів: ${total}`, { preserveText: true });
}

async function finishBroadcastIfReady(env, jobId) {
  const sql = sqlClient(env);
  const rows = await sql`UPDATE broadcast_jobs AS job SET
      status='complete',
      delivered=(SELECT COUNT(*)::int FROM broadcast_deliveries WHERE job_id=job.job_id AND status='sent'),
      failed=(SELECT COUNT(*)::int FROM broadcast_deliveries WHERE job_id=job.job_id AND status='failed'),
      updated_at=NOW()
    WHERE job.job_id=${jobId}::uuid
      AND job.status='running'
      AND job.fanout_complete=TRUE
      AND NOT EXISTS(
        SELECT 1 FROM broadcast_deliveries
        WHERE job_id=job.job_id AND status IN ('queued','sending')
      )
    RETURNING admin_chat_id,public_url,delivered,failed`;
  const completed = rows[0];
  if (!completed) return;
  try {
    await sendMain(
      env,
      Number(completed.admin_chat_id),
      Number(completed.admin_chat_id),
      completed.public_url,
      `✅ Розсилку завершено.\nДоставлено: ${completed.delivered}\nНе доставлено: ${completed.failed}`,
      { preserveText: true },
    );
  } catch (error) {
    console.error('Failed to send broadcast completion notice', cleanupError(error));
  }
}

async function fanOutBroadcast(env, body) {
  const jobId = String(body?.jobId || '');
  const cursor = String(body?.cursor || '0');
  if (!validBroadcastJobId(jobId) || !validTelegramId(cursor, true)) return;
  const sql = sqlClient(env);
  const jobs = await sql`SELECT status FROM broadcast_jobs WHERE job_id=${jobId}::uuid LIMIT 1`;
  if (!jobs[0] || jobs[0].status !== 'running') return;
  const rows = await sql`SELECT user_id FROM users
    WHERE user_id > ${cursor}::bigint
    ORDER BY user_id
    LIMIT ${BROADCAST_FANOUT_SIZE + 1}`;
  const page = rows.slice(0, BROADCAST_FANOUT_SIZE);
  if (page.length) {
    const userIds = page.map(row => String(row.user_id));
    await sql`INSERT INTO broadcast_deliveries(job_id,user_id,status,updated_at)
      SELECT ${jobId}::uuid,value::bigint,'queued',NOW()
      FROM jsonb_array_elements_text(${JSON.stringify(userIds)}::jsonb)
      ON CONFLICT(job_id,user_id) DO NOTHING`;
    await env.BROADCAST_QUEUE.sendBatch(userIds.map(userId => ({
      body: { kind: 'deliver', jobId, userId },
    })));
  }
  if (rows.length > BROADCAST_FANOUT_SIZE) {
    await env.BROADCAST_QUEUE.send({
      kind: 'fanout',
      jobId,
      cursor: String(page.at(-1).user_id),
    });
  } else {
    await sql`UPDATE broadcast_jobs SET fanout_complete=TRUE,updated_at=NOW()
      WHERE job_id=${jobId}::uuid AND status='running'`;
    await finishBroadcastIfReady(env, jobId);
  }
}

function broadcastRetryable(error) {
  const status = Number(error?.status || 0);
  return status === 0
    || status >= 500
    || status === 429
    || Number(error?.telegramCode || 0) === 429;
}

async function deliverBroadcast(env, body) {
  const jobId = String(body?.jobId || '');
  const userId = String(body?.userId || '');
  if (!validBroadcastJobId(jobId) || !validTelegramId(userId)) return;
  const sql = sqlClient(env);
  const claimedRows = await sql`WITH claimed AS (
      UPDATE broadcast_deliveries SET
        status='sending',
        attempts=attempts+1,
        lease_until=NOW() + INTERVAL '20 seconds',
        updated_at=NOW()
      WHERE job_id=${jobId}::uuid
        AND user_id=${userId}::bigint
        AND (status='queued' OR (status='sending' AND lease_until < NOW()))
      RETURNING attempts
    )
    SELECT claimed.attempts,job.message_text
    FROM claimed
    JOIN broadcast_jobs AS job ON job.job_id=${jobId}::uuid AND job.status='running'`;
  const claimed = claimedRows[0];
  if (!claimed) {
    await finishBroadcastIfReady(env, jobId);
    return;
  }
  try {
    await tg(env, 'sendMessage', {
      chat_id: Number(userId),
      text: `📢 Повідомлення адміністратора\n\n${claimed.message_text}`,
    }, { preserve: true });
    await sql`UPDATE broadcast_deliveries SET
      status='sent',lease_until=NULL,last_error=NULL,updated_at=NOW()
      WHERE job_id=${jobId}::uuid AND user_id=${userId}::bigint`;
  } catch (error) {
    const attempts = Number(claimed.attempts || 1);
    if (broadcastRetryable(error) && attempts < BROADCAST_MAX_ATTEMPTS) {
      await sql`UPDATE broadcast_deliveries SET
        status='queued',lease_until=NULL,last_error=${cleanupError(error)},updated_at=NOW()
        WHERE job_id=${jobId}::uuid AND user_id=${userId}::bigint`;
      error.retryAfter = Math.max(5, Number(error.retryAfter || 30));
      throw error;
    }
    await sql`UPDATE broadcast_deliveries SET
      status='failed',lease_until=NULL,last_error=${cleanupError(error)},updated_at=NOW()
      WHERE job_id=${jobId}::uuid AND user_id=${userId}::bigint`;
  }
  await finishBroadcastIfReady(env, jobId);
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
  if (data === 'users:noop') return;
  if (data.startsWith('users:list:')) {
    const [, , category, page = '0'] = data.split(':');
    return showCategory(env, chatId, category, Number(page));
  }
  if (data === 'users:export') return exportUsers(env, chatId);
  if (data.startsWith('manage:restore:')) {
    const id = Number(data.split(':').at(-1));
    if (!Number.isSafeInteger(id) || isAdmin(env, id)) return;
    await setStatus(env, id, 'approved');
    await resolveAdminNotifications(env, id);
    await tg(env, 'sendMessage', { chat_id: chatId, text: `✅ Доступ користувачу ${id} надано.` }, { preserve: true });
    try { await sendMain(env, id, id, url, '✅ Адміністратор надав вам доступ до DUGA.', { preserveText: true }); } catch (_) {}
    return;
  }
  if (data.startsWith('manage:revoke:')) {
    const id = Number(data.split(':').at(-1));
    if (!Number.isSafeInteger(id) || isAdmin(env, id)) return;
    await setStatus(env, id, 'blocked');
    await resolveAdminNotifications(env, id);
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

export function shouldDeleteIncomingMessage(env, msg, processed) {
  return Boolean(
    processed
    && msg?.chat?.type === 'private'
    && msg?.from
    && !isAdmin(env, msg.from.id),
  );
}

async function processTelegramUpdate(env, update, url) {
  if (update.callback_query) {
    const query = update.callback_query;
    const user = query.from;
    const chat = query.message?.chat;
    if (!user) return;
    if (chat && chat.type !== 'private') {
      try {
        await tg(env, 'answerCallbackQuery', {
          callback_query_id: query.id,
          text: 'Відкрийте DUGA у приватному чаті з ботом.',
          show_alert: true,
        }, { preserve: true });
      } catch (error) {
        console.warn('Failed to answer a non-private callback', cleanupError(error));
      }
      return;
    }
    const chatId = chat?.id || user.id;
    await cleanupTemporaryBotMessages(env, chatId);
    return handleCallback(env, query, url);
  }
  const msg = update.message || update.edited_message;
  if (!msg?.from || !msg.chat) return;
  if (msg.chat.type !== 'private') return;
  await cleanupTemporaryBotMessages(env, msg.chat.id);
  let processed = false;
  try {
    const result = await processTelegramMessage(env, msg, url);
    processed = true;
    return result;
  } finally {
    if (shouldDeleteIncomingMessage(env, msg, processed)) {
      await safeDeleteMessage(env, msg.chat.id, msg.message_id);
    }
  }
}

function toHex(bytes) {
  return [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function secureEqual(left, right) {
  const encoder = new TextEncoder();
  const [leftHash, rightHash] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(String(left))),
    crypto.subtle.digest('SHA-256', encoder.encode(String(right))),
  ]);
  const a = new Uint8Array(leftHash);
  const b = new Uint8Array(rightHash);
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a[index] ^ b[index];
  return difference === 0;
}

async function hmac(keyBytes, message) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', key, new TextEncoder().encode(message));
}

export async function verifyTelegramInitData(env, initData) {
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
  if (!await secureEqual(toHex(signature), hash.toLowerCase())) return null;
  const authDate = Number(params.get('auth_date') || 0);
  const ageSeconds = Date.now() / 1000 - authDate;
  if (!authDate || ageSeconds < -300 || ageSeconds > 3600) return null;
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

function json(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      ...extraHeaders,
    },
  });
}

const HTML_HEADERS = {
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'no-store',
  'Content-Security-Policy': "default-src 'none'; script-src 'self' 'unsafe-inline' https://telegram.org https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: https://unpkg.com https://tile.openstreetmap.org https://*.tile.openstreetmap.org; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'",
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Permissions-Policy': 'geolocation=(self), camera=(), microphone=()',
};

function appBootstrapHtml() {
  return `<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>DUGA</title><script src="https://telegram.org/js/telegram-web-app.js?61"></script><style>html,body{margin:0;min-height:100%;background:#0f1117;color:#fff;font:600 16px system-ui}main{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;text-align:center}</style></head><body><main id="status">Перевірка доступу…</main><script>(async()=>{const box=document.getElementById('status');const initData=window.Telegram?.WebApp?.initData||'';window.__DUGA_INIT_DATA__=initData;try{window.Telegram?.WebApp?.ready();window.Telegram?.WebApp?.expand();try{sessionStorage.setItem('duga:initData',initData)}catch(error){}const response=await fetch('/api/app',{headers:{'X-Telegram-Init-Data':initData},cache:'no-store'});if(!response.ok){const data=await response.json().catch(()=>({}));box.textContent=data.detail||'Доступ відсутній';return}const html=await response.text();document.open();document.write(html);document.close()}catch(error){box.textContent='Не вдалося перевірити доступ. Відкрийте DUGA з меню бота.'}})();</script></body></html>`;
}

export function authorizedAppHtml() {
  const authenticatedFetch = `<script>(function(){const rawFetch=window.fetch.bind(window);window.fetch=(input,init={})=>{try{const url=new URL(input instanceof Request?input.url:String(input),location.href);if(url.origin===location.origin&&url.pathname.startsWith('/api/')){let initData=window.__DUGA_INIT_DATA__||window.Telegram?.WebApp?.initData||'';try{initData=initData||sessionStorage.getItem('duga:initData')||''}catch(error){}const headers=new Headers(input instanceof Request?input.headers:init.headers||{});headers.set('X-Telegram-Init-Data',initData);init={...init,headers,cache:'no-store'}}}catch(error){}return rawFetch(input,init)}})();</script>`;
  return APP_HTML_RAW.replace('</head>', `${authenticatedFetch}</head>`);
}

async function acquireNominatimSlot(env) {
  const sql = sqlClient(env);
  const rows = await sql`INSERT INTO service_rate_limits(service,next_allowed_at)
    VALUES('nominatim',NOW() + INTERVAL '1 second')
    ON CONFLICT(service) DO UPDATE SET next_allowed_at=NOW() + INTERVAL '1 second'
    WHERE service_rate_limits.next_allowed_at <= NOW()
    RETURNING next_allowed_at`;
  return rows.length > 0;
}

async function sha256Hex(value) {
  return toHex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}

async function geocode(env, request, ctx) {
  const access = await webAppAccess(env, request);
  if (!access.ok) return json({ results: [], detail: access.detail }, access.status);
  const q = (new URL(request.url).searchParams.get('q') || '').trim();
  if (q.length < 2) return json({ results: [] });
  if (q.length > 200) return json({ results: [], detail: 'Запит надто довгий.' }, 400);
  if (!env.NOMINATIM_USER_AGENT) return json({ results: [], detail: 'Пошук адрес тимчасово не налаштований.' }, 503);
  const cacheKey = new Request(`${new URL(request.url).origin}/__cache/nominatim/${await sha256Hex(q.toLocaleLowerCase('uk'))}`);
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) return json(await cached.json());
  if (!await acquireNominatimSlot(env)) {
    return json({ results: [], detail: 'Зачекайте секунду та повторіть пошук.' }, 429, { 'Retry-After': '1' });
  }
  const endpoint = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=8&addressdetails=1&q=${encodeURIComponent(q)}`;
  try {
    const r = await fetch(endpoint, {
      headers: {
        'User-Agent': env.NOMINATIM_USER_AGENT,
        'Referer': baseUrl(env, request),
        'Accept-Language': 'uk,en;q=0.8',
      },
      signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) return json({ results: [], detail: 'Сервіс пошуку адрес тимчасово недоступний.' }, 503);
    const items = await r.json();
    const payload = { results: items.slice(0,8).map(x => ({ lat:Number(x.lat), lon:Number(x.lon), label:String(x.display_name || q), source:'OpenStreetMap' })).filter(x => Number.isFinite(x.lat) && Number.isFinite(x.lon)) };
    const cachedResponse = new Response(JSON.stringify(payload), {
      headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'public, max-age=86400' },
    });
    ctx.waitUntil(cache.put(cacheKey, cachedResponse));
    return json(payload);
  } catch (error) {
    console.warn('Nominatim request failed', cleanupError(error));
    return json({ results: [], detail: 'Сервіс пошуку адрес тимчасово недоступний.' }, 503);
  }
}

async function setupWebhook(env, request) {
  const authorization = request.headers.get('Authorization') || '';
  const key = authorization.startsWith('Bearer ') ? authorization.slice(7) : '';
  if (!env.SETUP_KEY || !key || !await secureEqual(key, env.SETUP_KEY)) return json({ ok:false, detail:'Forbidden' }, 403);
  if (!env.TELEGRAM_WEBHOOK_SECRET) return json({ ok:false, detail:'TELEGRAM_WEBHOOK_SECRET is required' }, 503);
  await migrateSchema(env);
  const url = baseUrl(env, request);
  const result = await tg(env, 'setWebhook', {
    url: `${url}/telegram-webhook`,
    allowed_updates: ['message','edited_message','callback_query'],
    drop_pending_updates: false,
    max_connections: 20,
    secret_token: env.TELEGRAM_WEBHOOK_SECRET,
  });
  return json({ ok:true, webhook:`${url}/telegram-webhook`, result });
}

async function processQueueMessage(env, body) {
  if (body?.kind === 'fanout') return fanOutBroadcast(env, body);
  if (body?.kind === 'deliver') return deliverBroadcast(env, body);
  console.warn('Ignored unknown broadcast queue message');
}

export default {
  async fetch(request, env, ctx) {
    const u = new URL(request.url);
    const path = u.pathname;
    try {
      if (path === '/' || path === '/app') return new Response(appBootstrapHtml(), { headers: HTML_HEADERS });
      if (path === '/live') return json({ status:'ok', service:'DUGA', runtime:'cloudflare-workers' });
      if (path === '/ready' || path === '/health') {
        const sql = sqlClient(env);
        const rows = await sql`SELECT
          to_regclass('public.users') IS NOT NULL AS users_ready,
          to_regclass('public.temporary_bot_messages') IS NOT NULL AS cleanup_ready,
          to_regclass('public.broadcast_jobs') IS NOT NULL AS broadcasts_ready,
          to_regclass('public.service_rate_limits') IS NOT NULL AS rate_limits_ready`;
        if (!Object.values(rows[0] || {}).every(Boolean)) return json({ status:'error', detail:'Database migration required' }, 503);
        return json({ status:'ok', service:'DUGA', database:'ok', runtime:'cloudflare-workers' });
      }
      if (path === '/api/app') {
        const access = await webAppAccess(env, request);
        if (!access.ok) return json({ ok:false, detail:access.detail }, access.status);
        return new Response(authorizedAppHtml(), { headers: HTML_HEADERS });
      }
      if (path === '/api/access') {
        const access = await webAppAccess(env, request);
        return json(access.ok ? { ok:true } : { ok:false, detail:access.detail }, access.ok ? 200 : access.status);
      }
      if (path === '/api/geocode') return geocode(env, request, ctx);
      if (path === '/admin/setup-webhook') {
        if (request.method !== 'POST') return json({ ok:false, detail:'Method not allowed' }, 405, { Allow: 'POST' });
        return setupWebhook(env, request);
      }
      if (path === '/telegram-webhook' && request.method === 'POST') {
        if (!env.TELEGRAM_WEBHOOK_SECRET) return json({ ok:false, detail:'Webhook secret is not configured' }, 503);
        const providedSecret = request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '';
        if (!providedSecret || !await secureEqual(providedSecret, env.TELEGRAM_WEBHOOK_SECRET)) return json({ ok:false }, 403);
        if (!String(request.headers.get('Content-Type') || '').toLowerCase().includes('application/json')) return json({ ok:false }, 415);
        const contentLength = Number(request.headers.get('Content-Length') || 0);
        if (contentLength > 1024 * 1024) return json({ ok:false }, 413);
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
  async queue(batch, env) {
    for (const message of batch.messages) {
      try {
        await processQueueMessage(env, message.body);
        message.ack();
      } catch (error) {
        console.error('DUGA broadcast queue error', error?.stack || error);
        message.retry({ delaySeconds: Math.min(300, Math.max(5, Number(error?.retryAfter || 30))) });
      }
    }
  },
};
