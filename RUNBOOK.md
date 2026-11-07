# Ava runbook

Short notes for running and changing the Ava voice agent. Settings live in `.env.local`
(never commit it; `.env.example` lists every setting).

## Run everything locally (Windows or Linux)

Open four terminals in the `brief-agent` folder:

```bash
uv run python mocks/internal_api.py      # 1. mock company API      (port 8081)
uv run python mocks/claude_webhook.py    # 2. mock Claude webhook   (port 8082)
lk agent dev --deployment dev-local      # 3. the agent (auto-reloads on code changes)
uv run python web/server.py              # 4. call page + token API (port 8090)
```

Then open http://localhost:8090 in Chrome on this computer, enter the team passcode
(`WEB_PASSCODE` in `.env.local`) once, and tap **Start call**.

- Always start the agent with `--deployment dev-local`. The web page dispatches Ava to the
  deployment named in `LIVEKIT_AGENT_DEPLOYMENT`; if the names differ, Ava never joins.
- To talk to Ava without the call page, use the Agent Console link that `lk agent dev`
  prints when it starts (it already points at the `dev-local` deployment).
- Phones need HTTPS for the microphone, so a phone can't use `http://<laptop-ip>:8090`.
  Phone testing happens after deployment to the server (Caddy adds HTTPS).

## Change things

| What | Where | Then |
|---|---|---|
| What Ava says and how (persona, rules, brief format) | `src/prompt.md` | restart agent |
| Greeting | `generate_reply(...)` near the end of `src/agent.py` | restart agent |
| Voice | `tts=inference.TTS(model=..., voice=...)` in `src/agent.py`. Voices: https://docs.livekit.io/agents/models/tts/ | restart agent |
| Knowledge base | add or edit `.md` files in `kb/` (split topics with `## ` headings) | restart agent |
| Data tools | `@function_tool` methods in `src/agent.py`; allowed API paths in `src/internal_api.py` (`ALLOWED_PATHS`) | restart agent |
| Live weather / time | `src/live_data.py` (Open-Meteo, no key; warehouse coordinates in `WAREHOUSES`); tools `get_weather`, `get_warehouse_weather`, `get_local_time` in `src/agent.py` | restart agent |
| Claude skill | `src/claude_skill.py` (skill name, URL, timeout); tool `run_risk_review` in `src/agent.py` | restart agent |
| Passcode | `WEB_PASSCODE` in the env file | restart web server |

`lk agent dev` restarts the agent automatically when you save a code file. For `kb/` or
`prompt.md` changes, stop it with Ctrl+C and start it again.

## Test without speaking

```bash
lk agent debugger start
lk agent debugger say "Give me my morning brief"
lk agent debugger stop
```

## Troubleshooting

- **Ava never joins the call**: the agent isn't running, or it runs under a different
  deployment name than `LIVEKIT_AGENT_DEPLOYMENT`. Check the agent log for `registered worker`.
- **"Microphone is blocked"**: allow microphone access for the site in the browser settings.
- **Choppy voice on a laptop**: keep `NOISE_CANCELLATION=off` locally.
- **"I couldn't reach the company systems"**: the internal API (or its mock) isn't running,
  or `INTERNAL_API_URL` / `INTERNAL_API_TOKEN` are wrong.
- **Port already in use**: change `WEB_PORT` in `.env.local`.
- **Call page shows "Reconnecting…"**: the network dropped for a moment. The browser reconnects
  by itself and Ava waits up to 20 seconds (`REJOIN_GRACE` in `src/agent.py`) before ending the call.
- **Slower or faster stand-in Claude**: set `MOCK_CLAUDE_DELAY` (seconds, default 15) before
  starting `mocks/claude_webhook.py`. `MOCK_CLAUDE_FAIL=1` makes it return an error.
- **Server: page doesn't load**: `systemctl status caddy ava-web` and `journalctl -u caddy`.
  The first HTTPS certificate can take a minute after deploy.
- **Server: Ava never joins**: `journalctl -u ava-agent -n 50` should show `registered worker`
  with deployment `production`.

## Server (Ubuntu)

On the server, Ava runs as four **systemd services** that start on boot and restart
automatically if they crash. **Caddy** serves the call page over HTTPS (phones need HTTPS
for the microphone) and renews the certificate by itself.

| Service | What it runs |
|---|---|
| `ava-agent` | the voice agent (`src/agent.py start`) |
| `ava-web` | the call page and token server (port 8090, behind Caddy) |
| `ava-mock-api` | sample company API (port 8081, local only) |
| `ava-mock-claude` | stand-in Claude webhook (port 8082, local only) |

Files on the server: app in `/opt/ava/app`, settings in `/etc/ava/ava.env` (readable only by
root and the `ava` user), HTTPS config in `/etc/caddy/Caddyfile`.

### First deploy and every update (from this Windows laptop)

```bash
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Server <server-ip>
```

It builds the server settings from `.env.local` (server values such as production
deployment and noise cancellation are switched automatically), uploads the app without
secrets or local files, installs everything, and prints the HTTPS address. Without a
domain it uses `<ip-with-dashes>.sslip.io`; add `-Domain ava.example.com` once you have a
domain pointing to the server.

### Day to day (on the server, `ssh -i ~/.ssh/ava_server ubuntu@<server-ip>`)

Use `root@` instead of `ubuntu@` on servers that log in as root (then drop `sudo`).

```bash
sudo systemctl restart ava-agent            # restart Ava (calls in progress finish first)
sudo systemctl restart ava-agent ava-web    # restart agent and call page
systemctl status ava-agent ava-web          # are they running?
journalctl -u ava-agent -f                  # live agent logs (Ctrl+C to stop)
journalctl -u ava-agent --since "1 hour ago"
journalctl -u ava-agent | grep "tools executed"   # which tools Ava used
sudo nano /etc/ava/ava.env                  # change a setting, then restart the service
```

To change the prompt, knowledge base or tools: edit them on the laptop, test locally, then
run `deploy.ps1` again.

To use the real company API or Claude setup instead of the mocks: set `INTERNAL_API_URL`,
`INTERNAL_API_TOKEN`, `CLAUDE_WEBHOOK_URL` and `CLAUDE_WEBHOOK_SECRET` in
`/etc/ava/ava.env`, restart `ava-agent`, and stop the mocks:
`sudo systemctl disable --now ava-mock-api ava-mock-claude`.
