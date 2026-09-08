import { neon } from '@neondatabase/serverless';
import analyticsWorker from './analytics_worker.js';
import baseWorker, { verifyTelegramInitData } from './worker.js';

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

async function ensureSearchSchema(env) {
  const sql = sqlClient(env);
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

async function authorizedUser(env, request) {
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

function normalizePoints(rawHeader) {
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
  return /^-?\d{1,3}(?:\.\d+)?\s*[,; ]\s*-?\d{1,3}(?:\.\d+)?$/.test(value)
    ? 'coordinates'
    : 'address_or_text';
}

async function saveSearchRequest(env, request, user, query) {
  await ensureSearchSchema(env);
  const sql = sqlClient(env);
  const points = normalizePoints(request.headers.get('X-DUGA-Search-Context') || '');
  await sql`INSERT INTO search_requests(user_id,query_text,query_type,points,created_at)
    VALUES(${Number(user.id)},${query.slice(0, 300)},${queryType(query)},${JSON.stringify(points)}::jsonb,NOW())`;
}

async function trackGeocodeEvent(env, userId) {
  try {
    const sql = sqlClient(env);
    await sql`CREATE TABLE IF NOT EXISTS analytics_events(
      id BIGSERIAL PRIMARY KEY,
      user_id BIGINT NOT NULL,
      event_type TEXT NOT NULL,
      metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
      is_admin BOOLEAN NOT NULL DEFAULT FALSE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )`;
    await sql`INSERT INTO analytics_events(user_id,event_type,metadata,is_admin,created_at)
      VALUES(${Number(userId)},'geocode_search','{}'::jsonb,${isAdmin(env, userId)},NOW())`;
  } catch (error) {
    console.warn('DUGA geocode analytics skipped', error instanceof Error ? error.message : String(error));
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === '/api/geocode') {
      const query = (url.searchParams.get('q') || '').trim();
      if (query.length >= 2) {
        try {
          const user = await authorizedUser(env, request);
          if (user) {
            // Persist before the external geocoding call so the request is never lost
            // because of cache, rate limits, upstream errors, or Worker lifetime.
            await saveSearchRequest(env, request, user, query);
            ctx.waitUntil(trackGeocodeEvent(env, user.id));
          }
        } catch (error) {
          console.error('DUGA request logging failed', error instanceof Error ? error.stack : error);
        }
      }
      // Bypass analyticsWorker only for geocoding to prevent duplicate search_requests rows.
      return baseWorker.fetch(request, env, ctx);
    }
    return analyticsWorker.fetch(request, env, ctx);
  },

  async queue(batch, env) {
    if (typeof analyticsWorker.queue === 'function') return analyticsWorker.queue(batch, env);
  },
};
