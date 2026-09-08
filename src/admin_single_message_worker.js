import { neon } from '@neondatabase/serverless';
import appWorker from './request_logging_worker.js';

const USERS_PAGE_SIZE = 5;

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

async function ensureState(env) {
  const sql = sqlClient(env);
  await sql`CREATE TABLE IF NOT EXISTS admin_single_message_state(
    admin_id BIGINT PRIMARY KEY,
    message_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )`;
}

async function previousMessage(env, adminId) {
  await ensureState(env);
  const sql = sqlClient(env);
  const rows = await sql`SELECT message_id FROM admin_single_message_state WHERE admin_id=${Number(adminId)} LIMIT 1`;
  return rows[0] ? Number(rows[0].message_id) : null;
}

async function rememberMessage(env, adminId, messageId) {
  if (!Number.isSafeInteger(Number(messageId))) return;
  await ensureState(env);
  const sql = sqlClient(env);
  await sql`INSERT INTO admin_single_message_state(admin_id,message_id,updated_at)
    VALUES(${Number(adminId)},${Number(messageId)},NOW())
    ON CONFLICT(admin_id) DO UPDATE SET message_id=EXCLUDED.message_id,updated_at=NOW()`;
}

async function telegram(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN is missing');
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok || !data?.ok) throw new Error(data?.description || `Telegram ${method} failed`);
  return data.result;
}

async function sendDocument(env, chatId, filename, text, caption, replyMarkup) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error('TELEGRAM_BOT_TOKEN is missing');
  const form = new FormData();
  form.append('chat_id', String(chatId));
  form.append('caption', caption);
  if (replyMarkup) form.append('reply_markup', JSON.stringify(replyMarkup));
  form.append('document', new Blob([text], { type: 'text/csv;charset=utf-8' }), filename);
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`, {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(15000),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok || !data?.ok) throw new Error(data?.description || `Telegram sendDocument failed`);
  return data.result;
}

async function deleteMessage(env, chatId, messageId) {
  if (!Number.isSafeInteger(Number(messageId))) return;
  try {
    await telegram(env, 'deleteMessage', { chat_id: Number(chatId), message_id: Number(messageId) });
  } catch (_) {}
}

async function deleteRange(env, chatId, from, to, except = null) {
  const ids = [];
  for (let id = Number(from); id <= Number(to); id++) {
    if (Number.isSafeInteger(id) && id > 0 && id !== Number(except)) ids.push(id);
    if (ids.length >= 100) break;
  }
  if (!ids.length) return;
  try {
    if (ids.length === 1) await telegram(env, 'deleteMessage', { chat_id: Number(chatId), message_id: ids[0] });
    else await telegram(env, 'deleteMessages', { chat_id: Number(chatId), message_ids: ids });
  } catch (_) {
    for (const id of ids) await deleteMessage(env, chatId, id);
  }
}

function publicBaseUrl(env, request) {
  return String(env.PUBLIC_BASE_URL || new URL(request.url).origin).replace(/\/$/, '');
}

async function sendUnifiedAdminMenu(env, chatId, request) {
  const url = publicBaseUrl(env, request);
  return telegram(env, 'sendMessage', {
    chat_id: Number(chatId),
    text: '🛡 DUGA · Адмін-панель',
    reply_markup: { inline_keyboard: [
      [{ text: '🚀 Запустити DUGA', web_app: { url: `${url}/app` } }],
      [{ text: '📊 Статистика', callback_data: 'analytics:period:1' }],
      [{ text: '👥 Користувачі', callback_data: 'users:categories' }],
      [{ text: '📢 Повідомлення користувачам', callback_data: 'main:broadcast' }],
      [{ text: '🔄 Перезапустити бота', callback_data: 'main:restart' }],
    ] },
  });
}

function updateUser(update) {
  return update?.callback_query?.from || update?.message?.from || update?.edited_message?.from || null;
}

async function setSingleMessage(env, chatId, adminId, finalMessageId, cleanupIds = []) {
  const previous = await previousMessage(env, adminId).catch(() => null);
  if (previous && previous !== finalMessageId) await deleteMessage(env, chatId, previous);
  for (const id of cleanupIds) {
    if (id !== finalMessageId && id !== previous) await deleteMessage(env, chatId, id);
  }
  await rememberMessage(env, adminId, finalMessageId).catch(() => {});
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

function categoryConfig(category) {
  if (category === 'pending') return { label: '⏳ Потребують дозволу' };
  if (category === 'approved') return { label: '✅ Надано доступ' };
  if (category === 'blocked') return { label: '⛔ Заблоковані' };
  return null;
}

async function categoryRows(env, category, limit, offset) {
  const sql = sqlClient(env);
  if (category === 'pending') {
    const count = await sql`SELECT COUNT(*)::int AS total FROM users WHERE status='pending' AND COALESCE(phone,'')<>''`;
    const rows = await sql`SELECT user_id,username,first_name,last_name,phone,status FROM users
      WHERE status='pending' AND COALESCE(phone,'')<>''
      ORDER BY updated_at DESC,user_id DESC LIMIT ${limit} OFFSET ${offset}`;
    return { total: Number(count[0]?.total || 0), rows };
  }
  if (category === 'approved') {
    const count = await sql`SELECT COUNT(*)::int AS total FROM users WHERE status='approved'`;
    const rows = await sql`SELECT user_id,username,first_name,last_name,phone,status FROM users
      WHERE status='approved'
      ORDER BY updated_at DESC,user_id DESC LIMIT ${limit} OFFSET ${offset}`;
    return { total: Number(count[0]?.total || 0), rows };
  }
  if (category === 'blocked') {
    const count = await sql`SELECT COUNT(*)::int AS total FROM users WHERE status='blocked'`;
    const rows = await sql`SELECT user_id,username,first_name,last_name,phone,status FROM users
      WHERE status='blocked'
      ORDER BY updated_at DESC,user_id DESC LIMIT ${limit} OFFSET ${offset}`;
    return { total: Number(count[0]?.total || 0), rows };
  }
  return { total: 0, rows: [] };
}

async function showUserCategory(env, callback, category, requestedPage = 0) {
  const config = categoryConfig(category);
  if (!config) throw new Error('Unknown user category');
  const chatId = Number(callback.message?.chat?.id || callback.from.id);
  const messageId = Number(callback.message?.message_id);
  const pageRequested = Math.max(0, Number(requestedPage) || 0);
  const preliminary = await categoryRows(env, category, USERS_PAGE_SIZE, pageRequested * USERS_PAGE_SIZE);
  const lastPage = Math.max(0, Math.ceil(preliminary.total / USERS_PAGE_SIZE) - 1);
  const page = Math.min(pageRequested, lastPage);
  const data = page === pageRequested
    ? preliminary
    : await categoryRows(env, category, USERS_PAGE_SIZE, page * USERS_PAGE_SIZE);

  const cards = data.rows.map((row, index) => {
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ').trim() || 'Без імені';
    const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : '—';
    const phone = row.phone || '—';
    const role = isAdmin(env, row.user_id) ? '🛡 Адміністратор' : config.label;
    return `<b>${page * USERS_PAGE_SIZE + index + 1}. ${escapeHtml(name)}</b>\n` +
      `Username: <code>${escapeHtml(username)}</code>\n` +
      `Телефон: <code>${escapeHtml(phone)}</code>\n` +
      `Telegram ID: <code>${escapeHtml(row.user_id)}</code>\n` +
      `Статус: ${escapeHtml(role)}`;
  });

  const buttons = [];
  for (const row of data.rows) {
    if (isAdmin(env, row.user_id)) continue;
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ').trim() || String(row.user_id);
    const shortName = name.length > 18 ? `${name.slice(0, 17)}…` : name;
    if (category === 'pending') buttons.push([
      { text: `✅ ${shortName}`, callback_data: `manage:restore:${row.user_id}` },
      { text: `⛔ ${shortName}`, callback_data: `manage:revoke:${row.user_id}` },
    ]);
    if (category === 'approved') buttons.push([
      { text: `⛔ ${shortName}`, callback_data: `manage:revoke:${row.user_id}` },
    ]);
    if (category === 'blocked') buttons.push([
      { text: `✅ ${shortName}`, callback_data: `manage:restore:${row.user_id}` },
    ]);
  }

  const nav = [];
  if (page > 0) nav.push({ text: '⬅️', callback_data: `users:list:${category}:${page - 1}` });
  nav.push({ text: `${page + 1}/${lastPage + 1}`, callback_data: 'users:noop' });
  if (page < lastPage) nav.push({ text: '➡️', callback_data: `users:list:${category}:${page + 1}` });
  buttons.push(nav);
  buttons.push([{ text: '⬅️ Назад', callback_data: 'users:categories' }]);

  const text = `<b>${config.label}</b>\nКількість: <b>${data.total}</b>\n\n${cards.join('\n\n') || 'Список порожній.'}`;
  await telegram(env, 'editMessageText', {
    chat_id: chatId,
    message_id: messageId,
    text,
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: buttons },
    disable_web_page_preview: true,
  });
  await rememberMessage(env, callback.from.id, messageId).catch(() => {});
}

async function exportUsers(env, callback) {
  const chatId = Number(callback.message?.chat?.id || callback.from.id);
  const sourceId = Number(callback.message?.message_id);
  const sql = sqlClient(env);
  const rows = await sql`SELECT user_id,username,first_name,last_name,phone,status,created_at,updated_at
    FROM users ORDER BY status,updated_at DESC,user_id DESC`;
  const quote = value => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const lines = [
    ['Ім\'я','Username','Телефон','Telegram ID','Статус','Створено','Оновлено'].map(quote).join(';'),
  ];
  for (const row of rows) {
    const name = [row.first_name, row.last_name].filter(Boolean).join(' ').trim() || 'Без імені';
    const username = row.username ? `@${String(row.username).replace(/^@/, '')}` : '';
    const status = row.status === 'approved' ? 'Надано доступ' : row.status === 'pending' ? 'Очікує дозволу' : 'Заблоковано';
    lines.push([name, username, row.phone || '', row.user_id, status, row.created_at, row.updated_at].map(quote).join(';'));
  }
  const filename = `DUGA_users_${new Date().toISOString().slice(0,16).replace(/[:T]/g,'-')}.csv`;
  const sent = await sendDocument(
    env,
    chatId,
    filename,
    '\uFEFF' + lines.join('\n'),
    `📋 Список користувачів DUGA · ${rows.length}`,
    { inline_keyboard: [[{ text: '⬅️ До користувачів', callback_data: 'users:categories' }]] },
  );
  const finalId = Number(sent?.message_id);
  if (Number.isSafeInteger(sourceId)) await deleteMessage(env, chatId, sourceId);
  await setSingleMessage(env, chatId, callback.from.id, finalId);
}

async function answerCallback(env, callbackId) {
  try {
    await telegram(env, 'answerCallbackQuery', { callback_query_id: callbackId });
  } catch (_) {}
}

async function secureEqual(left, right) {
  const encoder = new TextEncoder();
  const [aHash, bHash] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(String(left))),
    crypto.subtle.digest('SHA-256', encoder.encode(String(right))),
  ]);
  const a = new Uint8Array(aHash);
  const b = new Uint8Array(bHash);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function okResponse() {
  return new Response(JSON.stringify({ ok: true }), {
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

async function tryHandleAdminUserAction(env, request, update) {
  const callback = update?.callback_query;
  if (!callback?.from?.id || !isAdmin(env, callback.from.id)) return null;
  const data = String(callback.data || '');
  if (!(data === 'users:export' || data.startsWith('users:list:'))) return null;

  if (!env.TELEGRAM_WEBHOOK_SECRET) return null;
  const provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '';
  if (!provided || !await secureEqual(provided, env.TELEGRAM_WEBHOOK_SECRET)) {
    return new Response(JSON.stringify({ ok: false }), { status: 403, headers: { 'Content-Type': 'application/json' } });
  }

  await answerCallback(env, callback.id);
  try {
    if (data === 'users:export') {
      await exportUsers(env, callback);
      return okResponse();
    }
    const [, , category, rawPage = '0'] = data.split(':');
    await showUserCategory(env, callback, category, Number(rawPage));
    return okResponse();
  } catch (error) {
    console.error('DUGA direct admin user action failed', error instanceof Error ? error.stack : error);
    try {
      await telegram(env, 'editMessageText', {
        chat_id: Number(callback.message?.chat?.id || callback.from.id),
        message_id: Number(callback.message?.message_id),
        text: '⚠️ Не вдалося завантажити список користувачів. Повторіть спробу.',
        reply_markup: { inline_keyboard: [[{ text: '⬅️ Назад', callback_data: 'users:categories' }]] },
      });
    } catch (_) {}
    return okResponse();
  }
}

async function cleanAdminUpdate(env, request, update) {
  const user = updateUser(update);
  if (!user?.id || !isAdmin(env, user.id)) return;

  const callback = update.callback_query;
  if (callback) {
    const chatId = Number(callback.message?.chat?.id || user.id);
    const sourceId = Number(callback.message?.message_id);
    if (!Number.isSafeInteger(sourceId)) return;
    const data = String(callback.data || '');

    if (data.startsWith('analytics:')) {
      await setSingleMessage(env, chatId, user.id, sourceId);
      return;
    }

    if (['main:menu','main:back','main:restart','main:cancel','start_bot'].includes(data)) {
      const final = await sendUnifiedAdminMenu(env, chatId, request);
      const finalId = Number(final?.message_id);
      await deleteRange(env, chatId, sourceId, finalId - 1, finalId);
      await setSingleMessage(env, chatId, user.id, finalId);
      return;
    }

    let finalId = sourceId + 1;
    if (data.startsWith('manage:restore:') || data.startsWith('manage:revoke:')) finalId = sourceId + 2;
    if (data === 'users:noop') finalId = sourceId;

    if (finalId > sourceId) await deleteRange(env, chatId, sourceId, finalId - 1, finalId);
    await setSingleMessage(env, chatId, user.id, finalId);
    return;
  }

  const message = update.message || update.edited_message;
  if (!message) return;
  const chatId = Number(message.chat?.id || user.id);
  const sourceId = Number(message.message_id);
  if (!Number.isSafeInteger(sourceId)) return;
  const text = String(message.text || '').trim();

  if (text === '/stats' || text === '📊 Статистика') {
    const finalId = sourceId + 3;
    await deleteRange(env, chatId, sourceId + 1, finalId - 1, finalId);
    await setSingleMessage(env, chatId, user.id, finalId);
    return;
  }

  if (text === '/status') {
    const finalId = sourceId + 1;
    await setSingleMessage(env, chatId, user.id, finalId);
    return;
  }

  if (text === '/broadcast' || text === '📢 Повідомлення користувачам') {
    const finalId = sourceId + 1;
    await setSingleMessage(env, chatId, user.id, finalId);
    return;
  }

  if (text === '👥 Користувачі') {
    const finalId = sourceId + 1;
    await setSingleMessage(env, chatId, user.id, finalId);
    return;
  }

  const final = await sendUnifiedAdminMenu(env, chatId, request);
  const finalId = Number(final?.message_id);
  await deleteRange(env, chatId, sourceId + 1, finalId - 1, finalId);
  await setSingleMessage(env, chatId, user.id, finalId);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== '/telegram-webhook' || request.method !== 'POST') {
      return appWorker.fetch(request, env, ctx);
    }

    const copyForDirect = request.clone();
    const updateForDirect = await copyForDirect.json().catch(() => null);
    if (updateForDirect) {
      const direct = await tryHandleAdminUserAction(env, request, updateForDirect);
      if (direct) return direct;
    }

    const copy = request.clone();
    const response = await appWorker.fetch(request, env, ctx);
    if (!response.ok) return response;

    try {
      const update = await copy.json();
      await cleanAdminUpdate(env, request, update);
    } catch (error) {
      console.warn('DUGA admin single-message cleanup skipped', error instanceof Error ? error.message : String(error));
    }
    return response;
  },

  async queue(batch, env) {
    if (typeof appWorker.queue === 'function') return appWorker.queue(batch, env);
  },
};
