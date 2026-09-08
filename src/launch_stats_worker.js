import { neon } from '@neondatabase/serverless';
import appWorker from './admin_single_message_worker.js';

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

function periodConfig(raw) {
  const days = [1, 7, 30].includes(Number(raw)) ? Number(raw) : 1;
  return { days, label: days === 1 ? 'сьогодні' : `${days} днів`, offsetDays: days - 1 };
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function userName(row) {
  const full = [row?.first_name, row?.last_name].filter(Boolean).join(' ').trim();
  return full || (row?.username ? `@${String(row.username).replace(/^@/, '')}` : `ID ${row?.user_id || '—'}`);
}

function username(row) {
  return row?.username ? `@${String(row.username).replace(/^@/, '')}` : '—';
}

function kyivDateTime(value) {
  try {
    return new Intl.DateTimeFormat('uk-UA', {
      timeZone: 'Europe/Kyiv', day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date(value));
  } catch (_) {
    return String(value || '');
  }
}

async function telegram(env, method, payload) {
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

async function snapshot(env, rawDays) {
  const { days, label, offsetDays } = periodConfig(rawDays);
  const sql = sqlClient(env);
  const cutoffSql = `((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')`;

  const requestSummary = await sql`SELECT COUNT(*)::int AS total, COUNT(DISTINCT user_id)::int AS users
    FROM search_requests
    WHERE created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')`;

  const requestUsers = await sql`SELECT r.user_id,u.first_name,u.last_name,u.username,u.phone,COUNT(*)::int AS requests
    FROM search_requests r
    LEFT JOIN users u ON u.user_id=r.user_id
    WHERE r.created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')
    GROUP BY r.user_id,u.first_name,u.last_name,u.username,u.phone
    ORDER BY COUNT(*) DESC,MAX(r.created_at) DESC
    LIMIT 6`;

  const launchSummary = await sql`SELECT COUNT(*)::int AS launches, COUNT(DISTINCT user_id)::int AS users
    FROM analytics_events
    WHERE event_type='app_open'
      AND created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')`;

  const launchUsers = await sql`SELECT e.user_id,u.first_name,u.last_name,u.username,u.phone,
      COUNT(*)::int AS launches,MAX(e.created_at) AS last_open,
      BOOL_OR(e.is_admin)::boolean AS is_admin
    FROM analytics_events e
    LEFT JOIN users u ON u.user_id=e.user_id
    WHERE e.event_type='app_open'
      AND e.created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')
    GROUP BY e.user_id,u.first_name,u.last_name,u.username,u.phone
    ORDER BY MAX(e.created_at) DESC
    LIMIT 10`;

  return {
    days, label,
    requestTotal: Number(requestSummary[0]?.total || 0),
    requesters: Number(requestSummary[0]?.users || 0),
    requestUsers,
    launches: Number(launchSummary[0]?.launches || 0),
    launchers: Number(launchSummary[0]?.users || 0),
    launchUsers,
  };
}

function statsText(s) {
  const requestUsers = s.requestUsers.length
    ? s.requestUsers.map((row, i) => `${i + 1}. <b>${esc(userName(row))}</b> · ${esc(username(row))}\n   📱 <code>${esc(row.phone || '—')}</code> · 🔎 ${Number(row.requests || 0)}`).join('\n')
    : 'Ще немає користувачів із запитами.';

  const launchUsers = s.launchUsers.length
    ? s.launchUsers.map((row, i) => {
        const role = row.is_admin ? ' · 🛡' : '';
        return `${i + 1}. <b>${esc(userName(row))}</b>${role}\n` +
          `   ${esc(username(row))} · 📱 <code>${esc(row.phone || '—')}</code>\n` +
          `   🆔 <code>${esc(row.user_id)}</code> · 🚀 ${Number(row.launches || 0)} · 🕒 ${esc(kyivDateTime(row.last_open))}`;
      }).join('\n')
    : 'Ще ніхто не запускав програму за цей період.';

  return `📊 <b>DUGA — статистика</b>\nПеріод: <b>${s.label}</b>\n\n` +
    `🔎 <b>Запити</b>\n` +
    `Всього: <b>${s.requestTotal}</b> · користувачів: <b>${s.requesters}</b>\n\n` +
    `👤 <b>Хто робив запити</b>\n${requestUsers}\n\n` +
    `🚀 <b>Запуски програми</b>\n` +
    `Всього запусків: <b>${s.launches}</b> · користувачів: <b>${s.launchers}</b>\n\n` +
    `👥 <b>Хто запускав DUGA</b>\n${launchUsers}`;
}

function keyboard(days) {
  return { inline_keyboard: [
    [
      { text: days === 1 ? '● Сьогодні' : 'Сьогодні', callback_data: 'analytics:period:1' },
      { text: days === 7 ? '● 7 днів' : '7 днів', callback_data: 'analytics:period:7' },
      { text: days === 30 ? '● 30 днів' : '30 днів', callback_data: 'analytics:period:30' },
    ],
    [{ text: '📋 Деталі запитів', callback_data: `analytics:requests:${days}:0` }],
    [{ text: '🔄 Оновити', callback_data: `analytics:period:${days}` }],
    [{ text: '⬅️ Назад', callback_data: 'main:menu' }],
  ] };
}

async function renderStats(env, chatId, messageId, days) {
  const s = await snapshot(env, days);
  const payload = {
    chat_id: Number(chatId),
    text: statsText(s),
    parse_mode: 'HTML',
    reply_markup: keyboard(s.days),
    disable_web_page_preview: true,
  };
  if (Number.isSafeInteger(Number(messageId))) {
    try {
      return await telegram(env, 'editMessageText', { ...payload, message_id: Number(messageId) });
    } catch (error) {
      if (!String(error?.message || '').toLowerCase().includes('message is not modified')) throw error;
    }
  }
  return null;
}

function updateUser(update) {
  return update?.callback_query?.from || update?.message?.from || update?.edited_message?.from || null;
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
      const user = updateUser(update);
      const callback = update?.callback_query;
      if (user?.id && isAdmin(env, user.id) && callback) {
        const data = String(callback.data || '');
        if (data.startsWith('analytics:period:')) {
          const days = Number(data.split(':').at(-1));
          await renderStats(env, callback.message?.chat?.id || user.id, callback.message?.message_id, days);
        }
      }
    } catch (error) {
      console.warn('DUGA launch statistics enhancement skipped', error instanceof Error ? error.message : String(error));
    }

    return response;
  },

  async queue(batch, env) {
    if (typeof appWorker.queue === 'function') return appWorker.queue(batch, env);
  },
};
