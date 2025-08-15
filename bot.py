# bot.py — RushSearchBot: minimal panel + separate log channel (last 50)
import os
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction

# ---------- logging ----------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

# ---------- env ----------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))  # <-- apart log-kanaal (verplicht)

if not PANEL_CHANNEL_ID:
    raise RuntimeError("PANEL_CHANNEL_ID missing")
if not LOG_CHANNEL_ID:
    raise RuntimeError("LOG_CHANNEL_ID missing")

# ---------- constants ----------
LOG_LIMIT = 50
TZ = ZoneInfo("Europe/Amsterdam")  # voor HH:MM in log

# ---------- bot ----------
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ----- state per paneel-kanaal -----
    class PanelState:
        def __init__(self, panel_channel_id: int, log_channel_id: int):
            self.panel_channel_id: int = panel_channel_id
            self.log_channel_id: int = log_channel_id

            # panel
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # unix seconds
            self.queue: List[int] = []

            # log (één samenvattingsbericht in los log-kanaal)
            self.log_message_id: Optional[int] = None
            self.logbook: List[Tuple[int, int]] = []  # (user_id, ts)

            self.lock = asyncio.Lock()

        def panel_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.panel_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

        def log_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.log_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

    PANELS: Dict[int, PanelState] = {}
    def state() -> PanelState:
        # Eén paneel, vaste IDs uit env
        key = (PANEL_CHANNEL_ID, LOG_CHANNEL_ID)
        if key not in PANELS:
            PANELS[key] = PanelState(PANEL_CHANNEL_ID, LOG_CHANNEL_ID)
        return PANELS[key]

    # ----- UI -----
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

    # ----- panel rendering -----
    def panel_text(st: PanelState) -> str:
        lines: List[str] = []
        # Searching
        if st.current_user_id:
            if st.current_started_ts:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}> — <t:{st.current_started_ts}:t>")
            else:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")

        # lege regel
        lines.append("")

        # Queue (elke gebruiker op nieuwe regel)
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def send_panel_bottom(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {st.panel_channel_id} not found")
        # verwijder vorige panel
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(st), view=SearchView(st.panel_channel_id))
        st.panel_message_id = msg.id
        await ensure_log_message(st)
        return msg

    async def ensure_panel(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.panel_channel_id))
                await ensure_log_message(st)
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        # probeer te editen, anders opnieuw onderaan
        try:
            await inter.response.edit_message(content=panel_text(st), view=SearchView(st.panel_channel_id))
        except discord.NotFound:
            await send_panel_bottom(st)
            try:
                await inter.response.defer()
            except Exception:
                pass
        except Exception:
            try:
                await inter.response.defer()
            except Exception:
                pass

    # ----- log helpers (apart kanaal) -----
    def fmt_hhmm(ts_unix: int) -> str:
        dt = discord.utils.datetime.datetime.fromtimestamp(ts_unix, TZ)
        return dt.strftime("%H:%M")

    async def ensure_log_message(st: PanelState):
        ch = st.log_channel()
        if not ch:
            log.warning("Log channel %s not found or not TextChannel", st.log_channel_id)
            return
        if st.log_message_id:
            try:
                await ch.fetch_message(st.log_message_id)
                return
            except Exception:
                st.log_message_id = None
        # nieuw samenvattingsbericht
        msg = await ch.send("📜 **Log**\n*(empty)*")
        st.log_message_id = msg.id

    async def update_log_summary(st: PanelState):
        ch = st.log_channel()
        if not ch:
            return
        await ensure_log_message(st)
        try:
            msg = await ch.fetch_message(st.log_message_id)
        except Exception:
            # probeer opnieuw te maken
            await ensure_log_message(st)
            try:
                msg = await ch.fetch_message(st.log_message_id)
            except Exception:
                return

        last = st.logbook[-LOG_LIMIT:][::-1]  # nieuwste boven
        if last:
            rows = [f"{('<@'+str(uid)+'>'):<22} {fmt_hhmm(ts)}" for uid, ts in last]
            body = "\n".join(rows)
            text = f"📜 **Log**\n```{body}```"
        else:
            text = "📜 **Log**\n*(empty)*"

        try:
            await msg.edit(content=text)
        except Exception:
            pass

    # ----- state ops -----
    async def start_for(st: PanelState, user_id: int):
        st.current_user_id = user_id
        st.current_started_ts = int(discord.utils.utcnow().timestamp())
        try:
            st.queue.remove(user_id)
        except ValueError:
            pass

    async def stop_only(st: PanelState):
        st.current_user_id = None
        st.current_started_ts = None

    # ----- events -----
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        st = state()
        # paneel & log zeker stellen
        await ensure_panel(st)
        await ensure_log_message(st)
        await update_log_summary(st)
        log.info("Panel ensured in channel %s; Log in channel %s", PANEL_CHANNEL_ID, LOG_CHANNEL_ID)

    @bot.event
    async def on_message(message: discord.Message):
        # paneel onderaan houden in het paneel-kanaal
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id != PANEL_CHANNEL_ID:
            return
        st = state()
        try:
            async with st.lock:
                await send_panel_bottom(st)
        except Exception as e:
            log.warning("Move panel failed: %r", e)

    @bot.event
    async def on_interaction(interaction: Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        if ch.id != PANEL_CHANNEL_ID:
            return
        st = state()

        if cid == "rsb_search":
            await handle_search(interaction, st)
        elif cid == "rsb_found":
            await handle_found(interaction, st)
        elif cid == "rsb_next":
            await handle_next(interaction, st)
        elif cid == "rsb_reset":
            await handle_reset(interaction, st)

    # ----- handlers -----
    async def handle_search(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if st.current_user_id is None:
                await start_for(st, user.id)
            else:
                if user.id != st.current_user_id and user.id not in st.queue:
                    st.queue.append(user.id)
            await edit_panel_from_interaction(inter, st)

    async def handle_found(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if st.current_user_id and st.current_user_id == user.id:
                ts = int(discord.utils.utcnow().timestamp())
                st.logbook.append((user.id, ts))
                if len(st.logbook) > LOG_LIMIT:
                    st.logbook = st.logbook[-LOG_LIMIT:]
                await stop_only(st)
                await update_log_summary(st)  # update in log-kanaal
            await edit_panel_from_interaction(inter, st)

    async def handle_next(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if user.id != st.current_user_id and user.id not in st.queue:
                st.queue.append(user.id)
            await edit_panel_from_interaction(inter, st)

    async def handle_reset(inter: Interaction, st: PanelState):
        async with st.lock:
            if st.current_user_id:
                await stop_only(st)  # geen auto-start
            await edit_panel_from_interaction(inter, st)

    # ----- manual command (optional) -----
    @bot.command()
    async def panel(ctx: commands.Context):
        if ctx.channel.id != PANEL_CHANNEL_ID:
            return
        st = state()
        await ensure_panel(st)
        await ensure_log_message(st)
        await update_log_summary(st)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    return bot


async def main():
    bot = make_bot()
    await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Shutting down...")