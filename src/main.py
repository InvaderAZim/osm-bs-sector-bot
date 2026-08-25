from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from js import fetch
from workers import Response, WorkerEntrypoint


APP_HTML = (Path(__file__).parent / "app.html").read_text(encoding="utf-8")


def json_response(payload: dict, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        },
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        parsed = urlparse(request.url)
        path = parsed.path

        if path in {"/", "/app"}:
            return Response(
                APP_HTML,
                headers={
                    "Content-Type": "text/html; charset=utf-8",
                    "Cache-Control": "no-store",
                },
            )

        if path in {"/live", "/ready", "/health"}:
            return json_response(
                {
                    "status": "ok",
                    "service": "DUGA",
                    "runtime": "cloudflare-workers-python",
                }
            )

        if path == "/api/geocode":
            params = parse_qs(parsed.query)
            query = (params.get("q") or [""])[0].strip()
            if len(query) < 2:
                return json_response({"results": []})

            endpoint = (
                "https://nominatim.openstreetmap.org/search"
                f"?format=jsonv2&limit=8&addressdetails=1&q={quote(query)}"
            )
            try:
                upstream = await fetch(endpoint)
                if not upstream.ok:
                    return json_response({"results": []})
                items = await upstream.json()
                results = []
                for item in items:
                    try:
                        results.append(
                            {
                                "lat": float(item["lat"]),
                                "lon": float(item["lon"]),
                                "label": str(item.get("display_name") or query),
                                "source": "OpenStreetMap",
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                return json_response({"results": results[:8]})
            except Exception:
                return json_response({"results": []})

        if path == "/telegram-webhook":
            return json_response(
                {
                    "ok": False,
                    "detail": "Telegram webhook is intentionally not switched yet; Render remains the bot backend during migration.",
                },
                status=503,
            )

        return json_response({"detail": "Not found"}, status=404)
