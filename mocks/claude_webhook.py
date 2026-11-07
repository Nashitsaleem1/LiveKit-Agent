"""Mock of the internal Claude setup's webhook, for local development.

Run from the brief-agent folder:
    uv run python mocks/claude_webhook.py

POST http://127.0.0.1:8082/skills/run with a JSON body like
    {"request_id": "...", "skill": "ops_weekly_risk_review", "input": {"focus": "..."}}
and headers X-Timestamp and X-Signature, where
    X-Signature = hex(HMAC-SHA256(CLAUDE_WEBHOOK_SECRET, f"{timestamp}.{raw_body}"))

It waits MOCK_CLAUDE_DELAY seconds (default 15) to simulate Claude working, then returns a
canned result. Set MOCK_CLAUDE_FAIL=1 to simulate an error, or a large delay to test timeouts.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time

from aiohttp import web
from dotenv import load_dotenv

load_dotenv(".env.local")

SECRET = os.environ.get("CLAUDE_WEBHOOK_SECRET", "").encode()
PORT = int(os.environ.get("MOCK_CLAUDE_PORT", "8082"))
DELAY = float(os.environ.get("MOCK_CLAUDE_DELAY", "15"))
ALLOWED_SKILLS = {"ops_weekly_risk_review"}


def _verify(request: web.Request, body: bytes) -> bool:
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > 300:  # replay protection
        return False
    expected = hmac.new(SECRET, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def run_skill(request: web.Request) -> web.Response:
    body = await request.read()
    if not _verify(request, body):
        return web.json_response({"error": "invalid signature"}, status=401)
    payload = json.loads(body)
    if payload.get("skill") not in ALLOWED_SKILLS:
        return web.json_response({"error": "unknown skill"}, status=400)

    focus = (payload.get("input") or {}).get("focus") or "all operations"
    print(f"[mock claude] running {payload['skill']} (focus: {focus}) for {DELAY}s")
    await asyncio.sleep(DELAY)

    if os.environ.get("MOCK_CLAUDE_FAIL") == "1":
        return web.json_response({"error": "skill failed"}, status=502)

    return web.json_response(
        {
            "request_id": payload.get("request_id"),
            "status": "completed",
            "skill": payload["skill"],
            "result": {
                "focus": focus,
                "summary": (
                    "Operations are mostly healthy, but on-time delivery dipped below the "
                    "96 percent target on two days this week, driven by Dallas to Houston traffic "
                    "and late customer pickups."
                ),
                "top_risks": [
                    "Brightwell Foods volume is up 18 percent and Houston cold storage is at 91 percent capacity.",
                    "High-priority support tickets about the Harbor Track portal login doubled this week.",
                    "One Dallas forklift is out of service until Thursday, slowing cross-docking.",
                ],
                "recommendations": [
                    "Reserve overflow cold storage for Brightwell before the end of the month.",
                    "Ask IT to prioritize the portal login fix and post a status notice for customers.",
                    "Shift two afternoon Dallas loads to the morning shift until the forklift is back.",
                ],
            },
        }
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/skills/run", run_skill)
    return app


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit("Set CLAUDE_WEBHOOK_SECRET in .env.local first")
    web.run_app(make_app(), host="127.0.0.1", port=PORT)
