"""Read-only client for the internal company API.

Safety rules enforced in code (not just in the prompt):
- GET requests only.
- Only paths in ALLOWED_PATHS can be called.
- Short timeout, and failures become a ToolError the agent can explain to the user.

Configure with INTERNAL_API_URL and INTERNAL_API_TOKEN (use a read-only token).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from livekit.agents import utils
from livekit.agents.llm import ToolError

logger = logging.getLogger("internal_api")

ALLOWED_PATHS = {
    "/api/v1/brief/daily",
    "/api/v1/sales/summary",
    "/api/v1/support/tickets",
    "/api/v1/shipments/delayed",
    "/api/v1/calendar/today",
}

TIMEOUT = aiohttp.ClientTimeout(total=float(os.getenv("INTERNAL_API_TIMEOUT", "5")))


async def api_get(path: str, params: dict[str, str] | None = None) -> Any:
    if path not in ALLOWED_PATHS:
        # Programming error, never reachable from user input
        raise ToolError("That data source is not available.")

    base_url = os.getenv("INTERNAL_API_URL", "").rstrip("/")
    token = os.getenv("INTERNAL_API_TOKEN", "")
    if not base_url or not token:
        raise ToolError("Company data is not configured yet.")

    session = utils.http_context.http_session()
    try:
        async with session.get(
            f"{base_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status != 200:
                logger.warning("internal API %s returned %s", path, resp.status)
                raise ToolError(
                    "The company system returned an error. Try again later."
                )
            return await resp.json()
    except ToolError:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("internal API %s unreachable: %r", path, e)
        raise ToolError("I couldn't reach the company systems right now.") from e
