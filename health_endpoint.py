from __future__ import annotations

from datetime import datetime, timezone

from fastapi.responses import JSONResponse

import launcher as bot


@bot.api.get("/health", include_in_schema=False)
async def health_check() -> JSONResponse:
    """Lightweight endpoint for Render health checks and uptime monitoring."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "DUGA",
            "time": datetime.now(timezone.utc).isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )
