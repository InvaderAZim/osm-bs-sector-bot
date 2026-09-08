import { neon } from '@neondatabase/serverless';
import appWorker from './launch_stats_worker.js';

function sqlClient(env) {
  if (!env.DATABASE_URL) throw new Error('DATABASE_URL is missing');
  return neon(env.DATABASE_URL);
}

function adminIds(env) {
  return new Set(String(env.ADMIN_TELEGRAM_USER_IDS || '')
    .split(',')
    .map(value => Number(value.trim()))
    .filter(value => Number.isSafeInteger(value) && value > 0));
}

function isAdmin(env, userId) {
  return adminIds(env).has(Number(userId));
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

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    },
  });
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

async function safeTelegram(env, method, payload) {
  try {
    return await telegram(env, method, payload);
  } catch (error) {
    console.warn('DUGA Telegram best-effort action skipped', error instanceof Error ? error.message : String(error));
    return null;
  }
}

async function ensureProductionSchema(env) {
  const sql = sqlClient(env);
  await sql`CREATE TABLE IF NOT EXISTS admin_single_message_state(
    admin_id BIGINT PRIMARY KEY,
    message_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )`;
  await sql`CREATE TABLE IF NOT EXISTS analytics_events(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )`;
  await sql`CREATE INDEX IF NOT EXISTS idx_analytics_events_created_at
    ON analytics_events(created_at DESC)`;
  await sql`CREATE INDEX IF NOT EXISTS idx_analytics_events_type_created
    ON analytics_events(event_type,created_at DESC)`;
  await sql`CREATE TABLE IF NOT EXISTS search_requests(
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    query_text TEXT NOT NULL,
    query_type TEXT NOT NULL,
    points JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )`;
  await sql`CREATE INDEX IF NOT EXISTS idx_search_requests_created_at
    ON search_requests(created_at DESC)`;
  await sql`CREATE INDEX IF NOT EXISTS idx_search_requests_user_created
    ON search_requests(user_id,created_at DESC)`;
}

async function rememberAdminMessage(env, adminId, messageId) {
  if (!Number.isSafeInteger(Number(messageId))) return;
  await ensureProductionSchema(env);
  const sql = sqlClient(env);
  await sql`INSERT INTO admin_single_message_state(admin_id,message_id,updated_at)
    VALUES(${Number(adminId)},${Number(messageId)},NOW())
    ON CONFLICT(admin_id) DO UPDATE SET message_id=EXCLUDED.message_id,updated_at=NOW()`;
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

async function renderUsersOverview(env, callback, notice = '') {
  const chatId = Number(callback.message?.chat?.id || callback.from.id);
  const messageId = Number(callback.message?.message_id);
  const counts = await usersCounts(env);
  const prefix = notice ? `${notice}\n\n` : '';
  const payload = {
    chat_id: chatId,
    text: `${prefix}👥 <b>Керування користувачами</b>\n\nОберіть категорію:`,
    parse_mode: 'HTML',
    reply_markup: { inline_keyboard: [
      [{ text: `⏳ Потребують дозволу · ${Number(counts.pending || 0)}`, callback_data: 'users:list:pending' }],
      [{ text: `✅ Надано доступ · ${Number(counts.approved || 0)}`, callback_data: 'users:list:approved' }],
      [{ text: `⛔ Заблоковані · ${Number(counts.blocked || 0)}`, callback_data: 'users:list:blocked' }],
      [{ text: '📋 Завантажити список користувачів', callback_data: 'users:export' }],
      [{ text: '⬅️ Назад', callback_data: 'main:menu' }],
    ] },
  };

  if (Number.isSafeInteger(messageId) && !callback.message?.document) {
    try {
      await telegram(env, 'editMessageText', { ...payload, message_id: messageId });
      await rememberAdminMessage(env, callback.from.id, messageId);
      return;
    } catch (error) {
      if (!String(error?.message || '').toLowerCase().includes('message is not modified')) {
        console.warn('DUGA admin menu edit fallback', error instanceof Error ? error.message : String(error));
      } else {
        await rememberAdminMessage(env, callback.from.id, messageId);
        return;
      }
    }
  }

  const sent = await telegram(env, 'sendMessage', payload);
  const sentId = Number(sent?.message_id);
  if (Number.isSafeInteger(messageId) && messageId !== sentId) {
    await safeTelegram(env, 'deleteMessage', { chat_id: chatId, message_id: messageId });
  }
  await rememberAdminMessage(env, callback.from.id, sentId);
}

async function retireAdminNotifications(env, targetUserId, currentChatId, currentMessageId) {
  const sql = sqlClient(env);
  let rows = [];
  try {
    rows = await sql`SELECT admin_chat_id,message_id FROM admin_notifications
      WHERE user_id=${Number(targetUserId)} AND active=TRUE`;
  } catch (_) {
    return;
  }
  for (const row of rows) {
    const chatId = Number(row.admin_chat_id);
    const messageId = Number(row.message_id);
    if (chatId === Number(currentChatId) && messageId === Number(currentMessageId)) continue;
    await safeTelegram(env, 'editMessageReplyMarkup', {
      chat_id: chatId,
      message_id: messageId,
      reply_markup: { inline_keyboard: [[{ text: '⬅️ До користувачів', callback_data: 'users:categories' }]] },
    });
  }
  try {
    await sql`UPDATE admin_notifications SET active=FALSE WHERE user_id=${Number(targetUserId)} AND active=TRUE`;
  } catch (_) {}
}

async function handleManageCallback(env, request, update) {
  const callback = update?.callback_query;
  const data = String(callback?.data || '');
  if (!callback?.from?.id || !isAdmin(env, callback.from.id)) return null;
  if (!(data.startsWith('manage:restore:') || data.startsWith('manage:revoke:'))) return null;

  if (!env.TELEGRAM_WEBHOOK_SECRET) return null;
  const provided = request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '';
  if (!provided || !await secureEqual(provided, env.TELEGRAM_WEBHOOK_SECRET)) return json({ ok: false }, 403);

  await safeTelegram(env, 'answerCallbackQuery', { callback_query_id: callback.id });

  const targetUserId = Number(data.split(':').at(-1));
  if (!Number.isSafeInteger(targetUserId) || targetUserId <= 0 || isAdmin(env, targetUserId)) {
    await safeTelegram(env, 'answerCallbackQuery', {
      callback_query_id: callback.id,
      text: 'Некоректний користувач',
      show_alert: true,
    });
    return json({ ok: true });
  }

  const restoring = data.startsWith('manage:restore:');
  const nextStatus = restoring ? 'approved' : 'blocked';
  const sql = sqlClient(env);
  const rows = await sql`UPDATE users SET status=${nextStatus},updated_at=NOW()
    WHERE user_id=${targetUserId}
    RETURNING user_id,first_name,last_name,username,phone,status`;
  const target = rows[0];
  if (!target) {
    await renderUsersOverview(env, callback, '⚠️ Користувача не знайдено.');
    return json({ ok: true });
  }

  const currentChatId = Number(callback.message?.chat?.id || callback.from.id);
  const currentMessageId = Number(callback.message?.message_id);
  await retireAdminNotifications(env, targetUserId, currentChatId, currentMessageId);

  const targetText = restoring
    ? '✅ Адміністратор надав вам доступ до DUGA.'
    : '⛔ Ваш доступ до DUGA скасовано адміністратором.';
  await safeTelegram(env, 'sendMessage', { chat_id: targetUserId, text: targetText });

  await renderUsersOverview(
    env,
    callback,
    restoring ? '✅ Доступ користувачу надано.' : '⛔ Доступ користувачу скасовано.',
  );
  return json({ ok: true });
}

async function productionHealth(env, response) {
  if (!response.ok) return response;
  try {
    await ensureProductionSchema(env);
    const sql = sqlClient(env);
    const rows = await sql`SELECT
      to_regclass('public.users') IS NOT NULL AS users_ready,
      to_regclass('public.analytics_events') IS NOT NULL AS analytics_ready,
      to_regclass('public.search_requests') IS NOT NULL AS requests_ready,
      to_regclass('public.admin_single_message_state') IS NOT NULL AS admin_state_ready`;
    if (!Object.values(rows[0] || {}).every(Boolean)) {
      return json({ status: 'error', detail: 'Production database schema is incomplete' }, 503);
    }
    return response;
  } catch (error) {
    console.error('DUGA production health check failed', error instanceof Error ? error.stack : error);
    return json({ status: 'error', detail: 'Production database health check failed' }, 503);
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === '/telegram-webhook' && request.method === 'POST') {
      const copy = request.clone();
      const update = await copy.json().catch(() => null);
      if (update) {
        try {
          const handled = await handleManageCallback(env, request, update);
          if (handled) return handled;
        } catch (error) {
          console.error('DUGA hardened admin action failed', error instanceof Error ? error.stack : error);
          return json({ ok: true });
        }
      }
    }

    const response = await appWorker.fetch(request, env, ctx);
    if (url.pathname === '/health' || url.pathname === '/ready') return productionHealth(env, response);
    return response;
  },

  async queue(batch, env) {
    if (typeof appWorker.queue === 'function') return appWorker.queue(batch, env);
  },
};
