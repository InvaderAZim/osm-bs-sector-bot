from __future__ import annotations

from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import launcher as bot


def _database_ready() -> tuple[bool, str | None]:
    try:
        with bot.db() as connection:
            connection.execute("SELECT 1").fetchone()
        return True, None
    except Exception as exc:
        bot.log.exception("Readiness database check failed")
        return False, type(exc).__name__


@bot.api.get("/live", include_in_schema=False)
async def live_check() -> JSONResponse:
    """Cheap liveness probe: the HTTP process is running."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "DUGA",
            "time": datetime.now(timezone.utc).isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


@bot.api.get("/ready", include_in_schema=False)
async def ready_check() -> JSONResponse:
    """Readiness probe: critical persistent storage is reachable."""
    db_ok, db_error = await run_in_threadpool(_database_ready)
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "DUGA",
        "database": "ok" if db_ok else "error",
        "time": datetime.now(timezone.utc).isoformat(),
    }
    if db_error:
        payload["database_error"] = db_error
    return JSONResponse(
        payload,
        status_code=200 if db_ok else 503,
        headers={"Cache-Control": "no-store"},
    )


@bot.api.get("/health", include_in_schema=False)
async def health_check() -> JSONResponse:
    """Backward-compatible health endpoint with real dependency checking."""
    return await ready_check()
