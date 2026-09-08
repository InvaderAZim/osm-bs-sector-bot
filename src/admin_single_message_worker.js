import { neon } from '@neondatabase/serverless';
import appWorker from './request_logging_worker.js';

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
    if (data === 'users:export' || data.startsWith('manage:restore:') || data.startsWith('manage:revoke:')) finalId = sourceId + 2;
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
