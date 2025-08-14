# RushSearchBot (Render-ready)

A Discord bot with a button panel to coordinate "Search / Found / Next / Reset" with queueing and automatic handover.

## Deploy (Render)

1. Create a **Web Service** on Render pointing to this repo.
2. Add **Environment variables**:
   - `DISCORD_TOKEN` = your bot token (from Discord Developer Portal).
   - `PANEL_CHANNEL_ID` = numeric channel ID where the panel should auto-appear after restart.
   - Optional:
     - `SEARCH_TIMEOUT_SEC` (default 600)
     - `HANDOVER_DELAY_SEC` (default 10)
     - `SELF_PING` = `true`/`false` (default true)
3. Ensure **Privileged Gateway Intents** → **Message Content Intent** is enabled in the Developer Portal.
4. Files in this repo:
   - `bot.py` — main bot with Flask health endpoint and exponential backoff login.
   - `Procfile` — `web: python bot.py`
   - `requirements.txt`
   - `runtime.txt` — pins Python 3.12.6 on Render

## Commands
- `!panel` — place/refresh the button panel in the current channel
- `!status` — show current state/queue
- `!resetqueue` — clear queue and reset (requires Manage Messages permission)

## Notes
- The service exposes a simple health endpoint on `/` (Flask). Render detects port via `PORT`.
- Self-ping pings `http://127.0.0.1:<PORT>/` every 5 minutes (no Cloudflare noise).
- Login uses exponential backoff to survive temporary rate limiting (HTTP 429).
