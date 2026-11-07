"""Client for the internal Claude webhook.

The agent can trigger exactly one predefined, read-only skill (SKILL_NAME). The request is
signed with HMAC-SHA256 so the Claude side can verify it came from this agent:
    X-Timestamp: unix seconds
    X-Signature: hex(HMAC-SHA256(CLAUDE_WEBHOOK_SECRET, f"{timestamp}.{raw_body}"))

v1 is synchronous: the webhook holds the HTTP request open until Claude is done (up to
CLAUDE_WEBHOOK_TIMEOUT seconds). If real runs take longer, switch to a callback design.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

import aiohttp
from livekit.agents import utils
from livekit.agents.llm import ToolError

logger = logging.getLogger("claude_skill")

SKILL_NAME = "ops_weekly_risk_review"


async def run_skill(focus: str | None = None) -> dict[str, Any]:
    url = os.getenv("CLAUDE_WEBHOOK_URL", "")
    secret = os.getenv("CLAUDE_WEBHOOK_SECRET", "")
    if not url or not secret:
        raise ToolError("The analysis assistant is not configured yet.")

    request_id = str(uuid.uuid4())
    body = json.dumps(
        {
            "request_id": request_id,
            "skill": SKILL_NAME,
            "input": {"focus": (focus or "")[:120]},
        }
    ).encode()
    ts = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()

    timeout = aiohttp.ClientTimeout(
        total=float(os.getenv("CLAUDE_WEBHOOK_TIMEOUT", "90"))
    )
    session = utils.http_context.http_session()
    logger.info("calling Claude skill %s (request %s)", SKILL_NAME, request_id)
    try:
        async with session.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": ts,
                "X-Signature": signature,
            },
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Claude skill returned %s (request %s)", resp.status, request_id
                )
                raise ToolError("The analysis assistant reported an error.")
            data = await resp.json()
    except ToolError:
        raise
    except TimeoutError as e:
        raise ToolError("The analysis is taking too long and was stopped.") from e
    except aiohttp.ClientError as e:
        logger.warning("Claude skill unreachable: %r", e)
        raise ToolError("I couldn't reach the analysis assistant.") from e

    if data.get("status") != "completed" or "result" not in data:
        raise ToolError("The analysis assistant did not return a result.")
    return data["result"]
