"""Web server for the Ava call page.

Serves web/index.html and a token endpoint. The endpoint checks a shared passcode,
then returns a short-lived LiveKit token for a fresh room with Ava dispatched into it.

Run from the brief-agent folder:
    uv run python web/server.py

Settings (in .env.local locally, or the server's env file):
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   LiveKit Cloud project
    WEB_PASSCODE              shared passcode the team types once on their phone
    AGENT_NAME                agent to dispatch (default brief-agent)
    LIVEKIT_AGENT_DEPLOYMENT  deployment the agent registers under (empty = production)
    WEB_HOST / WEB_PORT       bind address (default 127.0.0.1:8090, Caddy proxies to it)
    TRUST_PROXY=1             read the client IP from X-Forwarded-For (only behind Caddy)
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

load_dotenv(".env.local")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web")

HERE = Path(__file__).resolve().parent
PASSCODE = os.environ.get("WEB_PASSCODE", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "brief-agent")
DEPLOYMENT = os.environ.get("LIVEKIT_AGENT_DEPLOYMENT", "")
TOKEN_TTL = timedelta(minutes=15)

# Simple brute-force protection: max failed passcode attempts per IP per window
MAX_FAILURES, WINDOW_S = 8, 600
_failures: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: web.Request) -> str:
    if os.environ.get("TRUST_PROXY") == "1":
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote or "unknown"


def _too_many_failures(ip: str) -> bool:
    q = _failures[ip]
    while q and q[0] < time.time() - WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILURES


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


async def token(request: web.Request) -> web.Response:
    ip = _client_ip(request)
    if _too_many_failures(ip):
        return web.json_response(
            {"error": "Too many attempts. Try again later."}, status=429
        )

    try:
        body = await request.json()
    except ValueError:
        body = {}
    passcode = str(body.get("passcode", ""))
    if not PASSCODE or not hmac.compare_digest(passcode.encode(), PASSCODE.encode()):
        _failures[ip].append(time.time())
        log.warning("wrong passcode from %s", ip)
        return web.json_response({"error": "Wrong passcode."}, status=401)

    room_name = f"brief-{secrets.token_hex(4)}"
    identity = f"web-{secrets.token_hex(4)}"
    jwt = (
        api.AccessToken()  # reads LIVEKIT_API_KEY / LIVEKIT_API_SECRET
        .with_identity(identity)
        .with_name("Team member")
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            api.RoomConfiguration(
                empty_timeout=30,
                departure_timeout=10,
                agents=[
                    api.RoomAgentDispatch(agent_name=AGENT_NAME, deployment=DEPLOYMENT)
                ],
            )
        )
        .to_jwt()
    )
    log.info(
        "token issued: room=%s deployment=%s ip=%s",
        room_name,
        DEPLOYMENT or "production",
        ip,
    )
    return web.json_response(
        {
            "server_url": os.environ["LIVEKIT_URL"],
            "participant_token": jwt,
            "room": room_name,
        },
        headers={"Cache-Control": "no-store"},
    )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application(client_max_size=4096)
    app.router.add_get("/", index)
    app.router.add_post("/api/token", token)
    app.router.add_get("/healthz", health)
    return app


if __name__ == "__main__":
    missing = [
        k
        for k in (
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "WEB_PASSCODE",
        )
        if not os.environ.get(k)
    ]
    if missing:
        raise SystemExit(f"Missing settings: {', '.join(missing)}")
    web.run_app(
        make_app(),
        host=os.environ.get("WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEB_PORT", "8090")),
    )
