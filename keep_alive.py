"""
keep_alive.py
─────────────
Cloudtype / Railway / Render 같은 플랫폼은 포트를 바인딩해야
컨테이너를 "살아있는" 서비스로 인식한다.
aiohttp 로 경량 HTTP 서버를 띄워 헬스체크 엔드포인트를 제공한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger(__name__)

# Cloudtype 기본 포트: 환경변수 PORT 가 없으면 8080 사용
PORT = int(os.getenv("PORT", 8080))


# ── 라우트 핸들러 ─────────────────────────────────────────────────────────────

async def handle_root(request: web.Request) -> web.Response:
    return web.Response(
        text="Discord VC Bot is running ✅",
        content_type="text/plain",
    )


async def handle_health(request: web.Request) -> web.Response:
    payload = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": _uptime_seconds(),
    }
    return web.json_response(payload)


_start_time = datetime.now(timezone.utc)


def _uptime_seconds() -> float:
    return (datetime.now(timezone.utc) - _start_time).total_seconds()


# ── 서버 팩토리 ───────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    return app


async def start_keep_alive() -> web.AppRunner:
    """
    asyncio 이벤트 루프 안에서 non-blocking 으로 웹 서버를 시작한다.
    bot.run() 보다 먼저 호출되어야 한다.
    """
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("[KeepAlive] HTTP server listening on port %d", PORT)
    return runner
