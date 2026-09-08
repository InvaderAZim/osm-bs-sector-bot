import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import worker from '../src/production_worker.js';

async function invoke(request, env = {}) {
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

describe('DUGA production worker', () => {
  it('loads the full production worker chain and serves live status', async () => {
    const response = await invoke(new Request('https://example.test/live'));
    expect(response.status).toBe(200);
    const payload = await response.json();
    expect(payload.status).toBe('ok');
    expect(payload.service).toBe('DUGA');
    expect(payload.runtime).toBe('cloudflare-workers');
  });

  it('keeps webhook authentication fail-closed through the production wrapper', async () => {
    const response = await invoke(new Request('https://example.test/telegram-webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Bot-Api-Secret-Token': 'wrong',
      },
      body: JSON.stringify({ update_id: 1 }),
    }), {
      TELEGRAM_WEBHOOK_SECRET: 'correct',
    });
    expect(response.status).toBe(403);
  });
});
