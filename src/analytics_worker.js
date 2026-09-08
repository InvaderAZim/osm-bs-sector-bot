import { neon } from '@neondatabase/serverless';
import baseWorker, { verifyTelegramInitData } from './worker.js';

const ANALYTICS_EVENTS = new Set([
  'app_open',
  'bot_message',
  'bot_callback',
  'bot_start',
  'geocode_search',
  'point_set',
  'point_tab',
  'azimuth_change',
  'radius_change',
  'clear_point',
  'fit_all',
  'common_polygon',
  'fullscreen',
]);

let analyticsSchemaPromise = null;

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

async function ensureAnalyticsSchema(env) {
  if (!analyticsSchemaPromise) {
    analyticsSchemaPromise = (async () => {
      const sql = sqlClient(env);
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
      await sql`CREATE INDEX IF NOT EXISTS idx_analytics_events_user_created
        ON analytics_events(user_id,created_at DESC)`;
      await sql`CREATE INDEX IF NOT EXISTS idx_analytics_events_type_created
        ON analytics_events(event_type,created_at DESC)`;
    })().catch(error => {
      analyticsSchemaPromise = null;
      throw error;
    });
  }
  return analyticsSchemaPromise;
}

function safeMetadata(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const output = {};
  for (const [key, raw] of Object.entries(value).slice(0, 8)) {
    if (!/^[a-zA-Z0-9_]{1,32}$/.test(key)) continue;
    if (typeof raw === 'number' && Number.isFinite(raw)) output[key] = raw;
    else if (typeof raw === 'boolean') output[key] = raw;
    else if (typeof raw === 'string') output[key] = raw.slice(0, 64);
  }
  return output;
}

async function trackEvent(env, userId, eventType, metadata = {}) {
  const id = Number(userId);
  if (!Number.isSafeInteger(id) || id <= 0 || !ANALYTICS_EVENTS.has(eventType)) return;
  try {
    await ensureAnalyticsSchema(env);
    const sql = sqlClient(env);
    const payload = JSON.stringify(safeMetadata(metadata));
    await sql`INSERT INTO analytics_events(user_id,event_type,metadata,is_admin,created_at)
      VALUES(${id},${eventType},${payload}::jsonb,${isAdmin(env, id)},NOW())`;
  } catch (error) {
    console.warn('DUGA analytics event skipped', error instanceof Error ? error.message : String(error));
  }
}

async function webUser(env, request) {
  const initData = request.headers.get('X-Telegram-Init-Data') || '';
  if (!initData) return null;
  const user = await verifyTelegramInitData(env, initData);
  if (!user?.id) return null;
  const sql = sqlClient(env);
  const rows = await sql`SELECT status,phone FROM users WHERE user_id=${Number(user.id)} LIMIT 1`;
  const row = rows[0];
  if (!row?.phone) return null;
  if (!(row.status === 'approved' || isAdmin(env, user.id))) return null;
  return user;
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

async function analyticsEventEndpoint(env, request) {
  if (request.method !== 'POST') return json({ ok: false, detail: 'Method not allowed' }, 405);
  const user = await webUser(env, request);
  if (!user) return json({ ok: false, detail: 'Unauthorized' }, 401);
  const length = Number(request.headers.get('Content-Length') || 0);
  if (length > 4096) return json({ ok: false, detail: 'Payload too large' }, 413);
  const body = await request.json().catch(() => null);
  const eventType = String(body?.event || '').trim();
  if (!ANALYTICS_EVENTS.has(eventType) || ['app_open','bot_message','bot_callback','bot_start','geocode_search'].includes(eventType)) {
    return json({ ok: false, detail: 'Unsupported event' }, 400);
  }
  await trackEvent(env, user.id, eventType, body?.meta);
  return json({ ok: true });
}

function analyticsClientScript() {
  return `<script>(function(){
    const last=new Map();
    function send(event,meta={}){
      const now=Date.now();
      if(now-(last.get(event)||0)<350)return;
      last.set(event,now);
      fetch('/api/analytics/event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event,meta}),keepalive:true}).catch(()=>{});
    }
    document.addEventListener('click',event=>{
      const button=event.target.closest('button');
      if(button){
        if(button.closest('#pointTabs')) return send('point_tab',{point:Number(button.dataset.i||0)+1});
        if(button.closest('#radii')) return send('radius_change',{km:Number(button.dataset.r||0)});
        if(button.id==='clearBtn') return send('clear_point');
        if(button.id==='fitBtn') return send('fit_all');
        if(button.id==='commonPolygonBtn') return send('common_polygon');
        if(button.id==='fullscreenBtn') return send('fullscreen');
      }
      const map=event.target.closest('#map');
      if(map && !event.target.closest('.leaflet-control')) send('point_set');
    },true);
    const az=document.getElementById('azimuth');
    if(az) az.addEventListener('change',()=>send('azimuth_change'));
  })();</script>`;
}

async function tgCall(env, method, payload = {}) {
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

function periodConfig(raw) {
  const days = [1, 7, 30, 90].includes(Number(raw)) ? Number(raw) : 7;
  return {
    days,
    label: days === 1 ? '24 години' : `${days} днів`,
    cutoff: new Date(Date.now() - days * 86400000).toISOString(),
  };
}

function number(value) {
  return Number(value || 0);
}

async function analyticsSnapshot(env, rawDays = 7) {
  await ensureAnalyticsSchema(env);
  const { days, label, cutoff } = periodConfig(rawDays);
  const sql = sqlClient(env);
  const userRows = await sql`SELECT
      COUNT(*)::int AS total,
      COUNT(*) FILTER (WHERE status='approved')::int AS approved,
      COUNT(*) FILTER (WHERE status='pending' AND COALESCE(phone,'')<>'')::int AS pending,
      COUNT(*) FILTER (WHERE status='blocked')::int AS blocked,
      COUNT(*) FILTER (WHERE created_at >= ${cutoff}::timestamptz)::int AS new_users
    FROM users`;
  const eventRows = await sql`SELECT
      COUNT(*) FILTER (WHERE is_admin=FALSE)::int AS events,
      COUNT(DISTINCT user_id) FILTER (WHERE is_admin=FALSE)::int AS active_users,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='app_open')::int AS app_opens,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='geocode_search')::int AS searches,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='point_set')::int AS points,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='common_polygon')::int AS polygons,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='fullscreen')::int AS fullscreen,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='bot_start')::int AS starts
    FROM analytics_events
    WHERE created_at >= ${cutoff}::timestamptz`;
  const returningRows = await sql`SELECT COUNT(*)::int AS returning_users FROM (
      SELECT user_id
      FROM analytics_events
      WHERE created_at >= ${cutoff}::timestamptz AND is_admin=FALSE
      GROUP BY user_id
      HAVING COUNT(DISTINCT created_at::date) >= 2
    ) AS returning`;
  const topRows = await sql`SELECT e.user_id,
      COALESCE(NULLIF(TRIM(CONCAT_WS(' ',u.first_name,u.last_name)),''),NULLIF(u.username,''),e.user_id::text) AS name,
      COUNT(*)::int AS events
    FROM analytics_events e
    LEFT JOIN users u ON u.user_id=e.user_id
    WHERE e.created_at >= ${cutoff}::timestamptz AND e.is_admin=FALSE
    GROUP BY e.user_id,u.first_name,u.last_name,u.username
    ORDER BY COUNT(*) DESC
    LIMIT 5`;
  const dailyRows = await sql`SELECT created_at::date AS day,
      COUNT(DISTINCT user_id) FILTER (WHERE is_admin=FALSE)::int AS active,
      COUNT(*) FILTER (WHERE is_admin=FALSE AND event_type='app_open')::int AS opens
    FROM analytics_events
    WHERE created_at >= ${cutoff}::timestamptz
    GROUP BY created_at::date
    ORDER BY day DESC
    LIMIT 7`;
  return {
    days,
    label,
    users: userRows[0] || {},
    events: eventRows[0] || {},
    returning: number(returningRows[0]?.returning_users),
    top: topRows,
    daily: dailyRows.reverse(),
  };
}

function analyticsText(snapshot) {
  const u = snapshot.users;
  const e = snapshot.events;
  const active = number(e.active_users);
  const events = number(e.events);
  const intensity = active ? (events / active).toFixed(1) : '0.0';
  const top = snapshot.top.length
    ? snapshot.top.map((row, index) => `${index + 1}. ${String(row.name).slice(0,28)} — ${number(row.events)}`).join('\n')
    : 'Ще немає даних.';
  const daily = snapshot.daily.length
    ? snapshot.daily.map(row => `${String(row.day).slice(5,10)} · 👤 ${number(row.active)} · 🚀 ${number(row.opens)}`).join('\n')
    : 'Ще немає даних.';
  return `📊 <b>DUGA — статистика та аналітика</b>\nПеріод: <b>${snapshot.label}</b>\n\n` +
    `👥 <b>Користувачі</b>\n` +
    `Всього: <b>${number(u.total)}</b>\n` +
    `✅ Доступ: ${number(u.approved)} · ⏳ Очікують: ${number(u.pending)} · ⛔ Заблоковано: ${number(u.blocked)}\n` +
    `🆕 Нові за період: <b>${number(u.new_users)}</b>\n\n` +
    `📱 <b>Використання без активності адмінів</b>\n` +
    `Активні користувачі: <b>${active}</b>\n` +
    `Повторно активні: <b>${snapshot.returning}</b>\n` +
    `🚀 Запуски Mini App: <b>${number(e.app_opens)}</b>\n` +
    `🔎 Пошуки адрес: <b>${number(e.searches)}</b>\n` +
    `📍 Встановлення точок: <b>${number(e.points)}</b>\n` +
    `⬡ Спільний полігон: <b>${number(e.polygons)}</b>\n` +
    `⛶ Fullscreen: <b>${number(e.fullscreen)}</b>\n` +
    `▶️ /start: <b>${number(e.starts)}</b>\n` +
    `Подій: <b>${events}</b> · на активного: <b>${intensity}</b>\n\n` +
    `📅 <b>Останні дні</b>\n${daily}\n\n` +
    `🏆 <b>Найактивніші</b>\n${top}`;
}

function analyticsKeyboard(days) {
  return { inline_keyboard: [
    [
      { text: days === 1 ? '● 24 год' : '24 год', callback_data: 'analytics:period:1' },
      { text: days === 7 ? '● 7 днів' : '7 днів', callback_data: 'analytics:period:7' },
    ],
    [
      { text: days === 30 ? '● 30 днів' : '30 днів', callback_data: 'analytics:period:30' },
      { text: days === 90 ? '● 90 днів' : '90 днів', callback_data: 'analytics:period:90' },
    ],
    [{ text: '🔄 Оновити', callback_data: `analytics:period:${days}` }],
    [{ text: '⬅️ Назад', callback_data: 'main:menu' }],
  ] };
}

async function sendAnalytics(env, chatId, days = 7, messageId = null) {
  const snapshot = await analyticsSnapshot(env, days);
  const payload = {
    chat_id: Number(chatId),
    text: analyticsText(snapshot),
    parse_mode: 'HTML',
    reply_markup: analyticsKeyboard(snapshot.days),
    disable_web_page_preview: true,
  };
  if (messageId) {
    return tgCall(env, 'editMessageText', { ...payload, message_id: Number(messageId) });
  }
  return tgCall(env, 'sendMessage', payload);
}

async function setAdminCommands(env, adminId) {
  try {
    await tgCall(env, 'setMyCommands', {
      scope: { type: 'chat', chat_id: Number(adminId) },
      commands: [
        { command: 'stats', description: 'Статистика та аналітика DUGA' },
        { command: 'status', description: 'Стан системи DUGA' },
      ],
    });
  } catch (error) {
    console.warn('Failed to set admin commands', error instanceof Error ? error.message : String(error));
  }
}

function updateUser(update) {
  return update?.callback_query?.from || update?.message?.from || update?.edited_message?.from || null;
}

async function postProcessTelegramUpdate(env, update, ctx) {
  const user = updateUser(update);
  if (!user?.id) return;
  const callback = update.callback_query;
  const message = update.message || update.edited_message;
  if (callback) {
    ctx.waitUntil(trackEvent(env, user.id, 'bot_callback'));
    const data = String(callback.data || '');
    if (isAdmin(env, user.id) && data.startsWith('analytics:period:')) {
      const days = Number(data.split(':').at(-1));
      try {
        await sendAnalytics(env, callback.message?.chat?.id || user.id, days, callback.message?.message_id || null);
      } catch (error) {
        console.error('DUGA analytics menu failed', error instanceof Error ? error.stack : error);
      }
    }
    return;
  }
  if (!message) return;
  const text = String(message.text || '').trim();
  ctx.waitUntil(trackEvent(env, user.id, text === '/start' || text.startsWith('/start ') ? 'bot_start' : 'bot_message'));
  if (!isAdmin(env, user.id)) return;
  if (text === '/start' || text.startsWith('/start ')) ctx.waitUntil(setAdminCommands(env, user.id));
  if (text === '/stats' || text === '📊 Статистика') {
    try {
      await sendAnalytics(env, message.chat?.id || user.id, 7);
    } catch (error) {
      console.error('DUGA analytics command failed', error instanceof Error ? error.stack : error);
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === '/api/analytics/event') return analyticsEventEndpoint(env, request);

    if (path === '/api/app') {
      const response = await baseWorker.fetch(request, env, ctx);
      if (!response.ok) return response;
      const user = await webUser(env, request);
      if (user) ctx.waitUntil(trackEvent(env, user.id, 'app_open'));
      const contentType = response.headers.get('Content-Type') || '';
      if (!contentType.includes('text/html')) return response;
      const html = await response.text();
      const headers = new Headers(response.headers);
      return new Response(html.replace('</body>', `${analyticsClientScript()}</body>`), {
        status: response.status,
        headers,
      });
    }

    if (path === '/api/geocode') {
      const userPromise = webUser(env, request).catch(() => null);
      const response = await baseWorker.fetch(request, env, ctx);
      const user = await userPromise;
      if (user && response.ok && (url.searchParams.get('q') || '').trim().length >= 2) {
        ctx.waitUntil(trackEvent(env, user.id, 'geocode_search'));
      }
      return response;
    }

    if (path === '/telegram-webhook' && request.method === 'POST') {
      const copy = request.clone();
      const response = await baseWorker.fetch(request, env, ctx);
      if (!response.ok) return response;
      const update = await copy.json().catch(() => null);
      if (update) {
        try {
          await postProcessTelegramUpdate(env, update, ctx);
        } catch (error) {
          console.error('DUGA analytics post-processing failed', error instanceof Error ? error.stack : error);
        }
      }
      return response;
    }

    if (path === '/health' || path === '/ready') {
      const response = await baseWorker.fetch(request, env, ctx);
      if (!response.ok) return response;
      try {
        await ensureAnalyticsSchema(env);
      } catch (_) {
        return json({ status: 'error', detail: 'Analytics database migration required' }, 503);
      }
      return response;
    }

    return baseWorker.fetch(request, env, ctx);
  },

  async queue(batch, env) {
    if (typeof baseWorker.queue === 'function') return baseWorker.queue(batch, env);
  },
};
