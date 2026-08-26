import { defineConfig } from 'vitest/config';

process.env.WRANGLER_LOG_PATH ||= '.wrangler/logs';
const { cloudflareTest } = await import('@cloudflare/vitest-plugin');

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
    }),
  ],
});
