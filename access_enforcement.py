from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

import launcher as bot
import mini_app


CHECK_SCRIPT = r'''
<script>
(() => {
  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  const originalFetch = window.fetch.bind(window);
  let blocked = false;

  function showBlocked(message) {
    blocked = true;
    let overlay = document.getElementById('access-block-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'access-block-overlay';
      overlay.style.cssText = 'position:fixed;inset:0;z-index:999999;background:#0f1117;color:#fff;display:flex;align-items:center;justify-content:center;padding:28px;text-align:center;font-family:system-ui,sans-serif';
      overlay.innerHTML = '<div><div style="font-size:52px;margin-bottom:14px">⛔</div><h2 style="margin:0 0 10px">Доступ скасовано</h2><p style="color:#aeb5c4;margin:0">' + (message || 'Зверніться до адміністратора бота.') + '</p></div>';
      document.body.appendChild(overlay);
    }
  }

  window.fetch = function(input, options = {}) {
    const url = typeof input === 'string' ? input : input?.url || '';
    if (url.startsWith('/api/')) {
      const headers = new Headers(options.headers || {});
      headers.set('X-Telegram-Init-Data', initData);
      options = {...options, headers};
    }
    return originalFetch(input, options).then(async response => {
      if (response.status === 401 || response.status === 403) {
        showBlocked('Ваш обліковий запис більше не має доступу до DUGA.');
      }
      return response;
    });
  };

  async function verifyAccess() {
    try {
      const response = await originalFetch('/api/access', {
        headers: {'X-Telegram-Init-Data': initData},
        cache: 'no-store'
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.allowed) {
        showBlocked(data.message || 'Ваш обліковий запис більше не має доступу до DUGA.');
      }
    } catch (_) {
      showBlocked('Не вдалося перевірити доступ. Відкрийте застосунок повторно з Telegram.');
    }
  }

  verifyAccess();
  setInterval(verifyAccess, 15000);
})();
</script>
'''


def _telegram_user(init_data: str) -> dict | None:
    if not init_data:
        return None

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot.settings().token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > 86400:
        return None

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if isinstance(user, dict) and user.get("id") else None


def _access_result(request: Request) -> tuple[bool, int | None, str]:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user = _telegram_user(init_data)
    if not user:
        return False, None, "Відкрийте DUGA кнопкою всередині Telegram."

    user_id = int(user["id"])
    if bot.is_admin(user_id):
        return True, user_id, ""

    row = bot.user_row(user_id)
    if not row or row["status"] != "approved":
        return False, user_id, "Доступ до застосунку скасовано адміністратором."

    return True, user_id, ""


@bot.api.middleware("http")
async def enforce_mini_app_access(request: Request, call_next):
    path = request.url.path

    if path == "/app":
        html = mini_app.APP_HTML.replace("</body>", CHECK_SCRIPT + "</body>")
        return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    if path == "/api/access":
        allowed, user_id, message = _access_result(request)
        status_code = 200 if allowed else 403
        return JSONResponse(
            {"allowed": allowed, "user_id": user_id, "message": message},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    if path.startswith("/api/"):
        allowed, _, message = _access_result(request)
        if not allowed:
            return JSONResponse({"error": "access_denied", "message": message}, status_code=403)

    return await call_next(request)
