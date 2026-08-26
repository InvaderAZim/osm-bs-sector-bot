import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import worker, {
  authorizedAppHtml,
  navigationKeyboard,
  shouldDeleteIncomingMessage,
  telegramInitDataFromUrl,
  verifyTelegramInitData,
} from '../src/worker.js';

async function invoke(request, env = {}) {
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

async function signedInitData(botToken, user, authDate = Math.floor(Date.now() / 1000)) {
  const params = new URLSearchParams({ auth_date: String(authDate), user: JSON.stringify(user) });
  const check = [...params.entries()].map(([key, value]) => `${key}=${value}`).sort().join('\n');
  const encoder = new TextEncoder();
  const importHmacKey = key => crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const secretKey = await importHmacKey(encoder.encode('WebAppData'));
  const secret = await crypto.subtle.sign('HMAC', secretKey, encoder.encode(botToken));
  const dataKey = await importHmacKey(secret);
  const signature = await crypto.subtle.sign('HMAC', dataKey, encoder.encode(check));
  const hash = [...new Uint8Array(signature)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  params.set('hash', hash);
  return params.toString();
}

describe('Telegram webhook security', () => {
  it('keeps only START in the persistent navigation row', () => {
    const keyboard = navigationKeyboard();
    expect(keyboard.is_persistent).toBe(true);
    expect(keyboard.keyboard).toEqual([[{ text: 'START' }]]);
  });

  it('fails closed when the webhook secret is missing', async () => {
    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }));
    expect(response.status).toBe(503);
  });

  it('rejects an invalid webhook secret', async () => {
    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'wrong',
      },
      body: '{}',
    }), { TELEGRAM_WEBHOOK_SECRET: 'correct' });
    expect(response.status).toBe(403);
  });

  it('ignores group messages before database or Telegram access', async () => {
    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'secret',
      },
      body: JSON.stringify({
        update_id: 1,
        message: {
          message_id: 2,
          from: { id: 3, first_name: 'User' },
          chat: { id: -4, type: 'group' },
          text: 'hello',
        },
      }),
    }), { TELEGRAM_WEBHOOK_SECRET: 'secret' });
    expect(response.status).toBe(200);
  });

  it('deletes only successfully processed private messages from ordinary users', () => {
    const ordinary = { from: { id: 10 }, chat: { id: 10, type: 'private' } };
    const admin = { from: { id: 20 }, chat: { id: 20, type: 'private' } };
    const group = { from: { id: 10 }, chat: { id: -30, type: 'group' } };
    expect(shouldDeleteIncomingMessage({}, ordinary, false)).toBe(false);
    expect(shouldDeleteIncomingMessage({}, ordinary, true)).toBe(true);
    expect(shouldDeleteIncomingMessage({ ADMIN_TELEGRAM_USER_IDS: '20' }, admin, true)).toBe(false);
    expect(shouldDeleteIncomingMessage({}, group, true)).toBe(false);
  });
});

describe('Telegram Mini App access', () => {
  it('serves only the authorization shell on the public app route', async () => {
    const response = await invoke(new Request('https://example.test/app'));
    const html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain("fetch('/api/app'");
    expect(html).toContain("get('tgWebAppData')");
    expect(html).not.toContain("L.map('map'");
  });

  it('recovers Telegram initData from the Mini App URL when the helper script is unavailable', () => {
    const initData = 'auth_date=123&user=%7B%22id%22%3A456%7D&hash=abc';
    const encoded = encodeURIComponent(initData);
    expect(telegramInitDataFromUrl(`https://example.test/app#tgWebAppData=${encoded}&tgWebAppVersion=9.1`)).toBe(initData);
    expect(telegramInitDataFromUrl(`https://example.test/app?tgWebAppData=${encoded}`)).toBe(initData);
    expect(telegramInitDataFromUrl('https://example.test/app', 'from-telegram-js')).toBe('from-telegram-js');
    expect(telegramInitDataFromUrl('not a url')).toBe('');
  });

  it('rejects direct app source access without Telegram initData', async () => {
    const response = await invoke(new Request('https://example.test/api/app'));
    expect(response.status).toBe(401);
  });

  it('renders geocoder labels without assigning untrusted HTML', () => {
    const html = authorizedAppHtml();
    expect(html).toContain('label.textContent=');
    expect(html).toContain("get('tgWebAppData')");
    expect(html).not.toContain('d.innerHTML=');
  });

  it('validates fresh initData and rejects stale initData', async () => {
    const env = { TELEGRAM_BOT_TOKEN: '123456:TEST_TOKEN' };
    const user = { id: 123, first_name: 'Test' };
    const fresh = await signedInitData(env.TELEGRAM_BOT_TOKEN, user);
    const stale = await signedInitData(env.TELEGRAM_BOT_TOKEN, user, Math.floor(Date.now() / 1000) - 7200);
    expect(await verifyTelegramInitData(env, fresh)).toEqual(user);
    expect(await verifyTelegramInitData(env, stale)).toBeNull();
  });
});
