# bot.py
import os
import asyncio
import logging
import threading
import random
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction
from flask import Flask

# ================== Config & Logging ==================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in environment variables.")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))  # set this in Render

# Minimal mode: no timeouts, no countdowns
SEARCH_TIMEOUT_SEC = None
HANDOVER_DELAY_SEC = 0

# Backoff for login retries (seconds)
BACKOFF_MIN = int(os.getenv("BACKOFF_MIN", "300"))
BACKOFF_MAX = int(os.getenv("BACKOFF_MAX", "900"))

# ================== Minimal Webserver (for Render health) ==================
app = Flask(__name__)

@app.get("/")
def health():
    return "ok", 200

def run_webserver():
    port = int(os.environ.get("PORT", "8080"))
    log.info("🌐 Starting webserver on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)

# ================== Bot factory ==================
def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True  # enable in Dev Portal too
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ---- Per-channel state ----
    class PanelState:
        def __init__(self, channel_id: int):
            self.channel_id: int = channel_id
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.queue: List[int] = []
            self.lock = asyncio.Lock()
            self.search_task: Optional[asyncio.Task] = None
            self.handover_task: Optional[asyncio.Task] = None

        def channel(self) -> Optional[discord.TextChannel]:
            return bot.get_channel(self.channel_id)

    PANELS: Dict[int, PanelState] = {}

    def get_or_create_state(channel_id: int) -> PanelState:
        if channel_id not in PANELS:
            PANELS[channel_id] = PanelState(channel_id)
        return PANELS[channel_id]

    # ---- UI view ----
    class SearchView(ui.View):
        def __init__(self, channel_id: int):
            super().__init__(timeout=None)  # persistent view
            self.channel_id = channel_id
            self.add_item(ui.Button(label="🔵 Search", style=ButtonStyle.primary,  custom_id="rsb_search"))
            self.add_item(ui.Button(label="✅ Found",  style=ButtonStyle.success,  custom_id="rsb_found"))
            self.add_item(ui.Button(label="🟡 Next",   style=ButtonStyle.secondary, custom_id="rsb_next"))
            self.add_item(ui.Button(label="🔁 Reset",  style=ButtonStyle.danger,    custom_id="rsb_reset"))

        async def interaction_check(self, interaction: Interaction) -> bool:
            # Only allow interactions from the same channel
            return interaction.channel and interaction.channel.id == self.channel_id

    # ---- Helpers ----
    def build_status_text(state: PanelState) -> str:
        parts = []
        if state.current_user_id:
            parts.append(f"🔎 **Searching**: <@{state.current_user_id}>")
        else:
            parts.append("🟦 **Searching**: *nobody*")

        if state.queue:
            preview = " → ".join(f"<@{uid}>" for uid in state.queue[:10])
            more = f" (+{len(state.queue)-10} more)" if len(state.queue) > 10 else ""
            parts.append(f"🟡 **Queue**: {preview}{more}")
        else:
            parts.append("🟡 **Queue**: *empty*")

        parts.append("\n**Buttons:** 🔵 *Search* — ✅ *Found* — 🟡 *Next* — 🔁 *Reset*")
        return "\n".join(parts)

    def build_view(channel_id: int) -> SearchView:
        return SearchView(channel_id)

    async def edit_panel_message(inter: Interaction, state: PanelState):
        """Fastest way: edit the interaction's message itself."""
        content = build_status_text(state)
        await inter.response.edit_message(content=content, view=build_view(state.channel_id))

    async def send_or_edit_panel(state: PanelState) -> discord.Message:
        """Used for initial placement or refresh outside interactions."""
        channel = state.channel()
        if not channel:
            raise RuntimeError(f"Channel {state.channel_id} not found or bot lacks access.")
        content = build_status_text(state)
        view = build_view(state.channel_id)

        if state.panel_message_id:
            try:
                msg = await channel.fetch_message(state.panel_message_id)
                await msg.edit(content=content, view=view)
                return msg
            except Exception:
                pass

        msg = await channel.send(content, view=view)
        state.panel_message_id = msg.id
        return msg

    async def start_search_for(state: PanelState, user_id: int):
        state.current_user_id = user_id
        # No timers in minimal mode
        if state.search_task and not state.search_task.done():
            state.search_task.cancel()
        if state.handover_task and not state.handover_task.done():
            state.handover_task.cancel()

    async def perform_handover(state: PanelState):
        state.current_user_id = None
        user_to_start = state.queue.pop(0) if state.queue else None
        if user_to_start:
            await start_search_for(state, user_to_start)

    # ---- Discord events ----
    @bot.event
    async def on_ready():
        try:
            log.info("✅ Logged in as %s (id=%s)", bot.user, bot.user.id)
        except Exception:
            log.info("✅ Logged in.")
        if PANEL_CHANNEL_ID:
            ch = bot.get_channel(PANEL_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                try:
                    state = get_or_create_state(ch.id)
                    await send_or_edit_panel(state)   # place/refresh panel silently
                    log.info("Panel ensured in channel %s", PANEL_CHANNEL_ID)
                except Exception as e:
                    log.error("Failed to ensure panel: %r", e)

    @bot.event
    async def on_interaction(interaction: Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            return

        state = get_or_create_state(channel.id)

        if cid == "rsb_search":
            await handle_search(interaction, state)
        elif cid == "rsb_found":
            await handle_found(interaction, state)
        elif cid == "rsb_next":
            await handle_next(interaction, state)
        elif cid == "rsb_reset":
            await handle_reset(interaction, state)

    # ---- Button handlers (no extra messages; only edit panel) ----
    async def handle_search(inter: Interaction, state: PanelState):
        user = inter.user
        async with state.lock:
            if state.current_user_id:
                if state.current_user_id == user.id:
                    # already searching -> just refresh panel (no text)
                    await edit_panel_message(inter, state)
                    return
                if user.id not in state.queue:
                    state.queue.append(user.id)
                await edit_panel_message(inter, state)
                return

            await start_search_for(state, user.id)
            await edit_panel_message(inter, state)

    async def handle_found(inter: Interaction, state: PanelState):
        user = inter.user
        if not state.current_user_id or state.current_user_id != user.id:
            # ignore silently (minimalistic)
            try:
                await inter.response.defer()  # acknowledge without message
            except Exception:
                pass
            return

        await perform_handover(state)
        await edit_panel_message(inter, state)

    async def handle_next(inter: Interaction, state: PanelState):
        user = inter.user
        if not state.current_user_id and not state.queue:
            # user becomes first immediately
            state.queue.append(user.id)
            await perform_handover(state)
            await edit_panel_message(inter, state)
            return

        if user.id not in state.queue:
            state.queue.append(user.id)
        await edit_panel_message(inter, state)

    async def handle_reset(inter: Interaction, state: PanelState):
        # Clear current and step to next if any
        state.current_user_id = None
        await perform_handover(state)
        await edit_panel_message(inter, state)

    # ---- Commands (silent/minimal) ----
    @bot.command()
    async def panel(ctx: commands.Context):
        """Place or refresh the panel in this channel (silent)."""
        state = get_or_create_state(ctx.channel.id)
        await send_or_edit_panel(state)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @bot.command()
    async def status(ctx: commands.Context):
        """Show current status by refreshing the panel only."""
        state = get_or_create_state(ctx.channel.id)
        await send_or_edit_panel(state)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @bot.command()
    @commands.has_permissions(manage_messages=True)
    async def resetqueue(ctx: commands.Context):
        """Clear queue and reset status (silent)."""
        state = get_or_create_state(ctx.channel.id)
        state.queue.clear()
        state.current_user_id = None
        if state.search_task and not state.search_task.done():
            state.search_task.cancel()
        if state.handover_task and not state.handover_task.done():
            state.handover_task.cancel()
        await send_or_edit_panel(state)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    return bot

# ================== Robust startup with backoff ==================
async def run_with_backoff(token: str):
    attempt = 0
    while True:
        bot = create_bot()  # fresh instance per attempt
        try:
            log.info("🔐 Starting Discord login...")
            await bot.start(token)  # login + connect
            break
        except discord.HTTPException as e:
            status = getattr(e, "status", None)
            retry_after = None
            try:
                if hasattr(e, "response") and e.response is not None:
                    ra = e.response.headers.get("Retry-After")
                    if ra is not None:
                        retry_after = float(ra)
            except Exception:
                pass

            if status == 429 or retry_after is not None:
                if retry_after is None:
                    delay_cap = min(BACKOFF_MAX, max(BACKOFF_MIN, 5 * (2 ** attempt)))
                    delay = random.uniform(BACKOFF_MIN, delay_cap)
                else:
                    delay = retry_after + random.uniform(0, 10)
                attempt += 1
                log.warning("⚠️ 429 rate limited. Sleeping for %.1f seconds (Retry-After=%s).", delay, retry_after)
                await asyncio.sleep(delay)
            else:
                log.error("HTTPException during start (status=%s): %r", status, e)
                await asyncio.sleep(30)
        except Exception as e:
            log.error("Unexpected error during start: %r", e)
            await asyncio.sleep(30)
        finally:
            try:
                await bot.close()
            except Exception:
                pass
            try:
                if hasattr(bot, "http") and getattr(bot.http, "session", None):
                    await bot.http.close()
            except Exception:
                pass

# ================== Main ==================
async def main():
    threading.Thread(target=run_webserver, daemon=True).start()
    await run_with_backoff(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Stop requested, shutting down...")