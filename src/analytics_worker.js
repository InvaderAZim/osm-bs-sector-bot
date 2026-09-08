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

const REQUEST_PAGE_SIZE = 4;
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
    const authenticatedFetch=window.fetch.bind(window);
    function pointSnapshot(){
      try{
        if(typeof points==='undefined'||!Array.isArray(points))return[];
        return points.slice(0,3).map((point,index)=>({
          point:index+1,
          lat:Number.isFinite(Number(point?.lat))?Number(point.lat):null,
          lon:Number.isFinite(Number(point?.lon))?Number(point.lon):null,
          azimuth:Number.isFinite(Number(point?.bearing))?Number(point.bearing):0,
          radius_km:Number.isFinite(Number(point?.radiusKm))?Number(point.radiusKm):0
        }));
      }catch(error){return[]}
    }
    window.fetch=(input,init={})=>{
      try{
        const url=new URL(input instanceof Request?input.url:String(input),location.href);
        if(url.origin===location.origin&&url.pathname==='/api/geocode'){
          const headers=new Headers(input instanceof Request?input.headers:init.headers||{});
          headers.set('X-DUGA-Search-Context',JSON.stringify({points:pointSnapshot()}));
          init={...init,headers};
        }
      }catch(error){}
      return authenticatedFetch(input,init);
    };
    function send(event,meta={}){
      const now=Date.now();
      if(now-(last.get(event)||0)<350)return;
      last.set(event,now);
      authenticatedFetch('/api/analytics/event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event,meta}),keepalive:true}).catch(()=>{});
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
      if(map&&!event.target.closest('.leaflet-control')) send('point_set');
    },true);
    const az=document.getElementById('azimuth');
    if(az) az.addEventListener('change',()=>send('azimuth_change'));
  })();</script>`;
}

function normalizeSearchPoints(rawHeader) {
  if (!rawHeader || rawHeader.length > 4096) return [];
  try {
    const parsed = JSON.parse(rawHeader);
    if (!Array.isArray(parsed?.points)) return [];
    return parsed.points.slice(0, 3).map((point, index) => {
      const lat = Number(point?.lat);
      const lon = Number(point?.lon);
      const azimuth = Number(point?.azimuth);
      const radius = Number(point?.radius_km);
      return {
        point: index + 1,
        lat: Number.isFinite(lat) && Math.abs(lat) <= 90 ? Number(lat.toFixed(7)) : null,
        lon: Number.isFinite(lon) && Math.abs(lon) <= 180 ? Number(lon.toFixed(7)) : null,
        azimuth: Number.isFinite(azimuth) ? ((Math.round(azimuth) % 360) + 360) % 360 : 0,
        radius_km: Number.isFinite(radius) && radius >= 0 && radius <= 100 ? Number(radius.toFixed(2)) : 0,
      };
    });
  } catch (_) {
    return [];
  }
}

function queryType(query) {
  const value = String(query || '').trim();
  if (/^-?\d{1,3}(?:\.\d+)?\s*[,; ]\s*-?\d{1,3}(?:\.\d+)?$/.test(value)) return 'coordinates';
  return 'address_or_text';
}

async function logSearchRequest(env, userId, query, request) {
  try {
    await ensureAnalyticsSchema(env);
    const sql = sqlClient(env);
    const points = normalizeSearchPoints(request.headers.get('X-DUGA-Search-Context') || '');
    const safeQuery = String(query || '').trim().slice(0, 300);
    if (!safeQuery) return;
    await sql`INSERT INTO search_requests(user_id,query_text,query_type,points,created_at)
      VALUES(${Number(userId)},${safeQuery},${queryType(safeQuery)},${JSON.stringify(points)}::jsonb,NOW())`;
  } catch (error) {
    console.warn('DUGA search request logging skipped', error instanceof Error ? error.message : String(error));
  }
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
  const days = [1, 7, 30].includes(Number(raw)) ? Number(raw) : 1;
  return {
    days,
    label: days === 1 ? 'сьогодні' : `${days} днів`,
    offsetDays: days - 1,
  };
}

function number(value) {
  return Number(value || 0);
}

function requesterName(row) {
  const fullName = [row?.first_name, row?.last_name].filter(Boolean).join(' ').trim();
  return fullName || (row?.username ? `@${String(row.username).replace(/^@/, '')}` : `ID ${row?.user_id || '—'}`);
}

function usernameLabel(row) {
  return row?.username ? `@${String(row.username).replace(/^@/, '')}` : '—';
}

function kyivDateTime(value) {
  try {
    return new Intl.DateTimeFormat('uk-UA', {
      timeZone: 'Europe/Kyiv',
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(value));
  } catch (_) {
    return String(value || '');
  }
}

function pointText(points) {
  if (!Array.isArray(points) || !points.length) return 'Точки не задані';
  return points.map((point, index) => {
    const n = Number(point?.point || index + 1);
    const coords = Number.isFinite(Number(point?.lat)) && Number.isFinite(Number(point?.lon))
      ? `${Number(point.lat).toFixed(6)}, ${Number(point.lon).toFixed(6)}`
      : 'не задана';
    const azimuth = Number.isFinite(Number(point?.azimuth)) ? `${Number(point.azimuth)}°` : '—';
    const radius = Number.isFinite(Number(point?.radius_km)) ? `${Number(point.radius_km)} км` : '—';
    return `P${n}: ${coords} · az ${azimuth} · r ${radius}`;
  }).join('\n');
}

async function requestStatsSnapshot(env, rawDays = 1) {
  await ensureAnalyticsSchema(env);
  const { days, label, offsetDays } = periodConfig(rawDays);
  const sql = sqlClient(env);
  const summaryRows = await sql`SELECT
      COUNT(*)::int AS total,
      COUNT(DISTINCT user_id)::int AS requesters
    FROM search_requests
    WHERE created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')`;
  const dailyRows = await sql`SELECT
      (created_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
      COUNT(*)::int AS requests
    FROM search_requests
    WHERE created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')
    GROUP BY (created_at AT TIME ZONE 'Europe/Kyiv')::date
    ORDER BY day DESC`;
  const requesterRows = await sql`SELECT
      r.user_id,u.first_name,u.last_name,u.username,u.phone,COUNT(*)::int AS requests
    FROM search_requests r
    LEFT JOIN users u ON u.user_id=r.user_id
    WHERE r.created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')
    GROUP BY r.user_id,u.first_name,u.last_name,u.username,u.phone
    ORDER BY COUNT(*) DESC,MAX(r.created_at) DESC
    LIMIT 8`;
  return {
    days,
    label,
    total: number(summaryRows[0]?.total),
    requesters: number(summaryRows[0]?.requesters),
    daily: dailyRows,
    users: requesterRows,
  };
}

function statsOverviewText(snapshot) {
  const daily = snapshot.daily.length
    ? snapshot.daily.map(row => `• ${String(row.day).slice(0,10)} — <b>${number(row.requests)}</b>`).join('\n')
    : 'Ще немає запитів.';
  const users = snapshot.users.length
    ? snapshot.users.map((row, index) => {
        const phone = row.phone || '—';
        return `${index + 1}. <b>${escapeHtml(requesterName(row))}</b> · ${escapeHtml(usernameLabel(row))}\n   📱 <code>${escapeHtml(phone)}</code> · запитів: <b>${number(row.requests)}</b>`;
      }).join('\n')
    : 'Ще немає користувачів із запитами.';
  return `📊 <b>DUGA — статистика запитів</b>\n` +
    `Період: <b>${snapshot.label}</b>\n\n` +
    `🔎 Всього запитів: <b>${snapshot.total}</b>\n` +
    `👥 Робили запити: <b>${snapshot.requesters}</b>\n\n` +
    `📅 <b>Запитів за день</b>\n${daily}\n\n` +
    `👤 <b>Хто робив запити</b>\n${users}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));
}

function statsKeyboard(days) {
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

async function sendAnalytics(env, chatId, days = 1, messageId = null) {
  const snapshot = await requestStatsSnapshot(env, days);
  const payload = {
    chat_id: Number(chatId),
    text: statsOverviewText(snapshot),
    parse_mode: 'HTML',
    reply_markup: statsKeyboard(snapshot.days),
    disable_web_page_preview: true,
  };
  if (messageId) return tgCall(env, 'editMessageText', { ...payload, message_id: Number(messageId) });
  return tgCall(env, 'sendMessage', payload);
}

async function requestDetails(env, rawDays = 1, requestedPage = 0) {
  await ensureAnalyticsSchema(env);
  const { days, label, offsetDays } = periodConfig(rawDays);
  const sql = sqlClient(env);
  const countRows = await sql`SELECT COUNT(*)::int AS total
    FROM search_requests
    WHERE created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')`;
  const total = number(countRows[0]?.total);
  const lastPage = Math.max(0, Math.ceil(total / REQUEST_PAGE_SIZE) - 1);
  const page = Math.min(Math.max(0, Number(requestedPage) || 0), lastPage);
  const offset = page * REQUEST_PAGE_SIZE;
  const rows = await sql`SELECT
      r.id,r.user_id,r.query_text,r.query_type,r.points,r.created_at,
      u.first_name,u.last_name,u.username,u.phone
    FROM search_requests r
    LEFT JOIN users u ON u.user_id=r.user_id
    WHERE r.created_at >= ((date_trunc('day', NOW() AT TIME ZONE 'Europe/Kyiv') - (${offsetDays} * INTERVAL '1 day')) AT TIME ZONE 'Europe/Kyiv')
    ORDER BY r.created_at DESC,r.id DESC
    LIMIT ${REQUEST_PAGE_SIZE} OFFSET ${offset}`;
  return { days, label, total, page, lastPage, rows };
}

function requestDetailsText(snapshot) {
  if (!snapshot.rows.length) return `📋 <b>Деталі запитів</b>\nПеріод: <b>${snapshot.label}</b>\n\nЗапитів ще немає.`;
  const cards = snapshot.rows.map((row, index) => {
    const type = row.query_type === 'coordinates' ? 'координати' : 'адреса / текст';
    return `<b>${snapshot.page * REQUEST_PAGE_SIZE + index + 1}. ${escapeHtml(kyivDateTime(row.created_at))}</b>\n` +
      `👤 ${escapeHtml(requesterName(row))}\n` +
      `🔗 ${escapeHtml(usernameLabel(row))}\n` +
      `📱 <code>${escapeHtml(row.phone || '—')}</code>\n` +
      `🔎 Тип: <b>${type}</b>\n` +
      `📝 <code>${escapeHtml(String(row.query_text || '').slice(0, 220))}</code>\n` +
      `📍 ${escapeHtml(pointText(row.points))}`;
  });
  return `📋 <b>Деталі запитів</b>\n` +
    `Період: <b>${snapshot.label}</b> · всього: <b>${snapshot.total}</b>\n` +
    `Сторінка: <b>${snapshot.page + 1}/${snapshot.lastPage + 1}</b>\n\n` +
    cards.join('\n\n');
}

function requestDetailsKeyboard(snapshot) {
  const nav = [];
  if (snapshot.page > 0) nav.push({ text: '⬅️', callback_data: `analytics:requests:${snapshot.days}:${snapshot.page - 1}` });
  nav.push({ text: `${snapshot.page + 1}/${snapshot.lastPage + 1}`, callback_data: 'analytics:noop' });
  if (snapshot.page < snapshot.lastPage) nav.push({ text: '➡️', callback_data: `analytics:requests:${snapshot.days}:${snapshot.page + 1}` });
  return { inline_keyboard: [
    nav,
    [{ text: '📊 До статистики', callback_data: `analytics:period:${snapshot.days}` }],
    [{ text: '⬅️ Назад', callback_data: 'main:menu' }],
  ] };
}

async function sendRequestDetails(env, chatId, days = 1, page = 0, messageId = null) {
  const snapshot = await requestDetails(env, days, page);
  const payload = {
    chat_id: Number(chatId),
    text: requestDetailsText(snapshot),
    parse_mode: 'HTML',
    reply_markup: requestDetailsKeyboard(snapshot),
    disable_web_page_preview: true,
  };
  if (messageId) return tgCall(env, 'editMessageText', { ...payload, message_id: Number(messageId) });
  return tgCall(env, 'sendMessage', payload);
}

async function sendAdminPanel(env, chatId) {
  return tgCall(env, 'sendMessage', {
    chat_id: Number(chatId),
    text: '🛡 Адмін-панель DUGA',
    reply_markup: { inline_keyboard: [
      [{ text: '📊 Статистика', callback_data: 'analytics:period:1' }],
    ] },
  });
}

async function setAdminCommands(env, adminId) {
  try {
    await tgCall(env, 'setMyCommands', {
      scope: { type: 'chat', chat_id: Number(adminId) },
      commands: [
        { command: 'stats', description: 'Статистика запитів DUGA' },
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
    if (!isAdmin(env, user.id)) return;
    if (data === 'analytics:noop') return;
    if (data.startsWith('analytics:period:')) {
      const days = Number(data.split(':').at(-1));
      try {
        await sendAnalytics(env, callback.message?.chat?.id || user.id, days, callback.message?.message_id || null);
      } catch (error) {
        console.error('DUGA analytics menu failed', error instanceof Error ? error.stack : error);
      }
      return;
    }
    if (data.startsWith('analytics:requests:')) {
      const [, , rawDays, rawPage] = data.split(':');
      try {
        await sendRequestDetails(env, callback.message?.chat?.id || user.id, Number(rawDays), Number(rawPage), callback.message?.message_id || null);
      } catch (error) {
        console.error('DUGA request details failed', error instanceof Error ? error.stack : error);
      }
    }
    return;
  }

  if (!message) return;
  const text = String(message.text || '').trim();
  ctx.waitUntil(trackEvent(env, user.id, text === '/start' || text.startsWith('/start ') ? 'bot_start' : 'bot_message'));
  if (!isAdmin(env, user.id)) return;
  if (text === '/start' || text.startsWith('/start ')) {
    ctx.waitUntil(setAdminCommands(env, user.id));
    try { await sendAdminPanel(env, message.chat?.id || user.id); } catch (_) {}
  }
  if (text === '/stats' || text === '📊 Статистика') {
    try {
      await sendAnalytics(env, message.chat?.id || user.id, 1);
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
      const query = (url.searchParams.get('q') || '').trim();
      if (user && response.ok && query.length >= 2) {
        ctx.waitUntil(Promise.all([
          trackEvent(env, user.id, 'geocode_search'),
          logSearchRequest(env, user.id, query, request),
        ]));
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
