import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import worker, {
  authorizedAppHtml,
  backRow,
  callbackBackTarget,
  callbackRequiresAdmin,
  mainKeyboard,
  navigationKeyboard,
  shouldDeleteIncomingMessage,
  telegramInitDataFromUrl,
  verifyTelegramInitData,
} from '../src/worker.js';

afterEach(() => {
  vi.unstubAllGlobals();
});

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
  it('removes the bottom keyboard when no contextual buttons are needed', () => {
    const keyboard = navigationKeyboard();
    expect(keyboard).toEqual({ remove_keyboard: true });
  });

  it('does not append START to contextual bottom buttons', () => {
    const rows = [[{ text: '📱 Надіслати свій контакт', request_contact: true }]];
    const keyboard = navigationKeyboard(rows);
    expect(keyboard.keyboard).toEqual(rows);
    expect(keyboard.keyboard.flat().some(button => button.text === 'START')).toBe(false);
  });

  it('keeps Back in submenus but not in the main menu', () => {
    expect(backRow('users:categories')).toEqual([{
      text: '⬅️ Назад',
      callback_data: 'users:categories',
    }]);
    const buttons = mainKeyboard({ ADMIN_TELEGRAM_USER_IDS: '20' }, 20, 'https://example.test')
      .inline_keyboard
      .flat();
    expect(buttons.some(button => button.text === '⬅️ Назад')).toBe(false);
    expect(buttons.some(button => button.callback_data === 'users:categories')).toBe(true);
  });

  it('returns failed submenu actions to the nearest usable menu', () => {
    expect(callbackBackTarget('users:categories')).toBe('main:menu');
    expect(callbackBackTarget('users:list:approved')).toBe('users:categories');
    expect(callbackBackTarget('manage:revoke:123')).toBe('users:categories');
    expect(callbackBackTarget('users:export')).toBe('users:categories');
    expect(callbackBackTarget('main:broadcast')).toBe('main:menu');
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

  it('restricts user-management callbacks to administrators', () => {
    expect(callbackRequiresAdmin('users:categories')).toBe(true);
    expect(callbackRequiresAdmin('manage:restore:123')).toBe(true);
    expect(callbackRequiresAdmin('main:broadcast')).toBe(true);
    expect(callbackRequiresAdmin('main:restart')).toBe(false);
  });

  it('does not fail the webhook when Telegram rejects a stale page callback', async () => {
    const telegramFetch = vi.fn(async () => new Response(JSON.stringify({
      ok: false,
      error_code: 400,
      description: 'Bad Request: query is too old',
    }), {
      status: 400,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', telegramFetch);

    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'secret',
      },
      body: JSON.stringify({
        update_id: 1,
        callback_query: {
          id: 'stale-callback',
          from: { id: 20, first_name: 'Admin' },
          message: {
            message_id: 5,
            chat: { id: 20, type: 'private' },
          },
          data: 'users:noop',
        },
      }),
    }), {
      ADMIN_TELEGRAM_USER_IDS: '20',
      TELEGRAM_BOT_TOKEN: '123456:TEST_TOKEN',
      TELEGRAM_WEBHOOK_SECRET: 'secret',
    });

    expect(response.status).toBe(200);
    expect(telegramFetch).toHaveBeenCalledTimes(1);
    expect(telegramFetch.mock.calls[0][0]).toContain('/answerCallbackQuery');
  });

  it('continues the callback action when Telegram cannot acknowledge it', async () => {
    let messageId = 10;
    const telegramFetch = vi.fn(async url => {
      if (String(url).includes('/answerCallbackQuery')) {
        return new Response(JSON.stringify({
          ok: false,
          error_code: 400,
          description: 'Bad Request: query is too old',
        }), {
          status: 400,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      messageId += 1;
      return new Response(JSON.stringify({
        ok: true,
        result: { message_id: messageId },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', telegramFetch);

    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'secret',
      },
      body: JSON.stringify({
        update_id: 2,
        callback_query: {
          id: 'stale-restart-callback',
          from: { id: 30, first_name: 'User' },
          message: {
            message_id: 10,
            chat: { id: 30, type: 'private' },
          },
          data: 'main:restart',
        },
      }),
    }), {
      TELEGRAM_BOT_TOKEN: '123456:TEST_TOKEN',
      TELEGRAM_WEBHOOK_SECRET: 'secret',
    });

    expect(response.status).toBe(200);
    expect(telegramFetch).toHaveBeenCalledTimes(3);
    expect(telegramFetch.mock.calls.map(call => String(call[0]))).toEqual([
      expect.stringContaining('/answerCallbackQuery'),
      expect.stringContaining('/sendMessage'),
      expect.stringContaining('/sendMessage'),
    ]);
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
