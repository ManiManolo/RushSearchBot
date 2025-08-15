# bot.py (Worker variant - no Flask/HTTP)
import os
import asyncio
import logging
import random
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction

# ===== Logging =====
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in environment variables.")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
BACKOFF_MIN = int(os.getenv("BACKOFF_MIN", "300"))
BACKOFF_MAX = int(os.getenv("BACKOFF_MAX", "900"))

def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    class PanelState:
        def __init__(self, channel_id: int):
            self.channel_id: int = channel_id
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.queue: List[int] = []
            self.lock = asyncio.Lock()

        def channel(self) -> Optional[discord.TextChannel]:
            return bot.get_channel(self.channel_id)

    PANELS: Dict[int, PanelState] = {}

    def state_for(channel_id: int) -> PanelState:
        if channel_id not in PANELS:
            PANELS[channel_id] = PanelState(channel_id)
        return PANELS[channel_id]

    class SearchView(ui.View):
        def __init__(self, channel_id: int):
            super().__init__(timeout=None)
            self.channel_id = channel_id
            self.add_item(ui.Button(label="🔵 Search", style=ButtonStyle.primary,  custom_id="rsb_search"))
            self.add_item(ui.Button(label="✅ Found",  style=ButtonStyle.success,  custom_id="rsb_found"))
            self.add_item(ui.Button(label="🟡 Next",   style=ButtonStyle.secondary, custom_id="rsb_next"))
            self.add_item(ui.Button(label="🔁 Reset",  style=ButtonStyle.danger,    custom_id="rsb_reset"))

        async def interaction_check(self, interaction: Interaction) -> bool:
            return interaction.channel and interaction.channel.id == self.channel_id

    def panel_text(st: PanelState) -> str:
        lines = []
        lines.append(f"🔎 **Searching**: <@{st.current_user_id}>" if st.current_user_id else "🟦 **Searching**: *nobody*")
        if st.queue:
            preview = " → ".join(f"<@{uid}>" for uid in st.queue[:10])
            more = f" (+{len(st.queue)-10})" if len(st.queue) > 10 else ""
            lines.append(f"🟡 **Queue**: {preview}{more}")
        else:
            lines.append("🟡 **Queue**: *empty*")
        return "\n".join(lines)

    async def ensure_panel(st: PanelState):
        ch = st.channel()
        if not ch:
            raise RuntimeError(f"Channel {st.channel_id} not found or access denied.")
        try:
            if st.panel_message_id:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.channel_id))
                return
        except Exception:
            pass
        msg = await ch.send(panel_text(st), view=SearchView(st.channel_id))
        st.panel_message_id = msg.id

    async def edit_from_interaction(inter: Interaction, st: PanelState):
        await inter.response.edit_message(content=panel_text(st), view=SearchView(st.channel_id))

    async def start_for(st: PanelState, user_id: int):
        st.current_user_id = user_id

    async def handover(st: PanelState):
        st.current_user_id = None
        if st.queue:
            nxt = st.queue.pop(0)
            await start_for(st, nxt)

    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        if PANEL_CHANNEL_ID:
            ch = bot.get_channel(PANEL_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ensure_panel(state_for(ch.id))
                    log.info("Panel ensured in channel %s", PANEL_CHANNEL_ID)
                except Exception as e:
                    log.error("Ensure panel failed: %r", e)

    @bot.event
    async def on_interaction(inter: Interaction):
        if inter.type != discord.InteractionType.component:
            return
        cid = inter.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        ch = inter.channel
        if not isinstance(ch, discord.TextChannel):
            return
        st = state_for(ch.id)

        if cid == "rsb_search":
            await handle_search(inter, st)
        elif cid == "rsb_found":
            await handle_found(inter, st)
        elif cid == "rsb_next":
            await handle_next(inter, st)
        elif cid == "rsb_reset":
            await handle_reset(inter, st)

    async def handle_search(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if st.current_user_id:
                if user.id not in st.queue and st.current_user_id != user.id:
                    st.queue.append(user.id)
            else:
                await start_for(st, user.id)
            await edit_from_interaction(inter, st)

    async def handle_found(inter: Interaction, st: PanelState):
        user = inter.user
        if st.current_user_id and st.current_user_id == user.id:
            await handover(st)
            await edit_from_interaction(inter, st)
        else:
            try:
                await inter.response.defer()
            except Exception:
                pass

    async def handle_next(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if not st.current_user_id and not st.queue:
                await start_for(st, user.id)
            else:
                if user.id not in st.queue:
                    st.queue.append(user.id)
            await edit_from_interaction(inter, st)

    async def handle_reset(inter: Interaction, st: PanelState):
        async with st.lock:
            if st.current_user_id:
                await handover(st)  # anyone can reset current searcher
            await edit_from_interaction(inter, st)

    @bot.command()
    async def panel(ctx: commands.Context):
        st = state_for(ctx.channel.id)
        await ensure_panel(st)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @bot.command()
    @commands.has_permissions(manage_messages=True)
    async def resetqueue(ctx: commands.Context):
        st = state_for(ctx.channel.id)
        st.queue.clear()
        st.current_user_id = None
        await ensure_panel(st)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    return bot

async def run_with_backoff(token: str):
    attempt = 0
    while True:
        bot = create_bot()
        try:
            logging.getLogger("rushsearchbot").info("🔐 Starting Discord login...")
            await bot.start(token)
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
                cap = min(BACKOFF_MAX, max(BACKOFF_MIN, 5 * (2 ** attempt)))
                delay = (retry_after + random.uniform(0, 10)) if retry_after is not None else random.uniform(BACKOFF_MIN, cap)
                attempt += 1
                logging.getLogger("rushsearchbot").warning("⚠️ 429 rate limited. Sleeping for %.1f s (Retry-After=%s).", delay, retry_after)
                await asyncio.sleep(delay)
            else:
                logging.getLogger("rushsearchbot").error("HTTPException during start (status=%s): %r", status, e)
                await asyncio.sleep(30)
        except Exception as e:
            logging.getLogger("rushsearchbot").error("Unexpected error during start: %r", e)
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

if __name__ == "__main__":
    try:
        asyncio.run(run_with_backoff(TOKEN))
    except KeyboardInterrupt:
        logging.getLogger("rushsearchbot").info("🛑 Shutting down...")