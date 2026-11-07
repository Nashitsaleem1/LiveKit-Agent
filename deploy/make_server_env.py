"""Build the server's settings file from your local .env.local.

Usage: python deploy/make_server_env.py <local .env.local> <output file>

Keeps your keys and secrets, and switches the values that differ on the server.
"""

import sys
from pathlib import Path

from dotenv import dotenv_values

SERVER_OVERRIDES = {
    "LIVEKIT_AGENT_DEPLOYMENT": "",  # empty = production, which the call page dispatches to
    "NOISE_CANCELLATION": "on",  # the server has the CPU for it; helps in the car
    "TRUST_PROXY": "1",  # Caddy sits in front, so read the real client IP from it
    "WEB_HOST": "127.0.0.1",  # only Caddy talks to the web server
    "WEB_PORT": "8090",
    "INTERNAL_API_URL": "http://127.0.0.1:8081",
    "CLAUDE_WEBHOOK_URL": "http://127.0.0.1:8082/skills/run",
}
REQUIRED = [
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
    "WEB_PASSCODE", "INTERNAL_API_TOKEN", "CLAUDE_WEBHOOK_SECRET",
]  # fmt: skip

values = {k: v or "" for k, v in dotenv_values(sys.argv[1]).items()}
missing = [k for k in REQUIRED if not values.get(k)]
if missing:
    raise SystemExit(f"Missing in {sys.argv[1]}: {', '.join(missing)}")
values.update(SERVER_OVERRIDES)
Path(sys.argv[2]).write_text(
    "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8", newline="\n"
)
print(f"server env written with {len(values)} settings")
