# bot.py — RushSearchBot: minimal panel + thread "Log" (last 50)
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

# ---------- constants ----------
LOG_THREAD_NAME = "Log"
LOG_LIMIT = 50
TZ = ZoneInfo("Europe/Amsterdam")  # voor logtijd HH:MM

# ---------- bot ----------
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ----- state per channel -----
    class PanelState:
        def __init__(self, channel_id: int):
            self.channel_id: int = channel_id

            # panel
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # unix seconds
            self.queue: List[int] = []

            # log (thread + summary message)
            self.thread_id: Optional[int] = None
            self.thread_summary_message_id: Optional[int] = None
            self.logbook: List[Tuple[int, int]] = []  # (user_id, ts)

            self.lock = asyncio.Lock()

        def channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

    PANELS: Dict[int, PanelState] = {}

    def state_for(cid: int) -> PanelState:
        if cid not in PANELS:
            PANELS[cid] = PanelState(cid)
        return PANELS[cid]

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
                # alleen tijd (lokaal voor elke gebruiker via Discord-render)
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}> — <t:{st.current_started_ts}:t>")
            else:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")

        # lege regel tussen delen
        lines.append("")

        # Queue (elke gebruiker op nieuwe regel)
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def send_panel_bottom(st: PanelState):
        ch = st.channel()
        if not ch:
            raise RuntimeError(f"Channel {st.channel_id} not found")
        # verwijder vorige panel als die er is
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(st), view=SearchView(st.channel_id))
        st.panel_message_id = msg.id
        await ensure_log_thread(st)
        return msg

    async def ensure_panel(st: PanelState):
        ch = st.channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.channel_id))
                await ensure_log_thread(st)
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        # Probeer het paneel te editen; zo niet, plaats ‘m onderaan opnieuw.
        try:
            await inter.response.edit_message(content=panel_text(st), view=SearchView(st.channel_id))
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

    # ----- log thread helpers -----
    async def ensure_log_thread(st: PanelState) -> Optional[discord.Thread]:
        """Zoek of maak de thread 'Log' en zorg voor een (één) samenvattingsbericht."""
        ch = st.channel()
        if not ch:
            return None

        # 1) Hebben we al een thread-id?
        if st.thread_id:
            th = bot.get_channel(st.thread_id)
            if isinstance(th, discord.Thread):
                if th.archived:
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                # check of het summary-bericht er nog is
                if st.thread_summary_message_id:
                    try:
                        await th.fetch_message(st.thread_summary_message_id)
                    except Exception:
                        st.thread_summary_message_id = None
                if st.thread_summary_message_id is None:
                    # aanmaken als missend
                    msg = await th.send("📜 **Log**\n*(empty)*")
                    st.thread_summary_message_id = msg.id
                return th

        # 2) Probeer een bestaande actieve thread met de juiste naam te vinden
        try:
            for th in ch.threads:
                if isinstance(th, discord.Thread) and th.name == LOG_THREAD_NAME:
                    st.thread_id = th.id
                    # summary check/aanmaak
                    if st.thread_summary_message_id is None:
                        msg = await th.send("📜 **Log**\n*(empty)*")
                        st.thread_summary_message_id = msg.id
                    return th
        except Exception:
            pass

        # 3) Maak een nieuwe public thread
        try:
            th = await ch.create_thread(
                name=LOG_THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=4320  # 3 dagen
            )
            st.thread_id = th.id
            msg = await th.send("📜 **Log**\n*(empty)*")
            st.thread_summary_message_id = msg.id
            return th
        except Exception as e:
            log.warning("Failed to create/find log thread: %r", e)
            return None

    def fmt_hm(ts_unix: int) -> str:
        # Format HH:MM in Europe/Amsterdam (zonder extra packages)
        dt = discord.utils.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        # Beter: gebruik ts_unix zelf
        dt = discord.utils.datetime.datetime.fromtimestamp(ts_unix, TZ)
        return dt.strftime("%H:%M")

    async def update_log_summary(st: PanelState):
        """Edit één samenvattingsbericht in de 'Log' thread met laatste 50."""
        th = await ensure_log_thread(st)
        if not th:
            return

        last = st.logbook[-LOG_LIMIT:][::-1]  # nieuwste bovenaan
        if last:
            # compact: 2 kolommen in codeblok (mention links, tijd rechts)
            rows = [f"{('<@'+str(uid)+'>'):<22} {fmt_hm(ts)}" for uid, ts in last]
            body = "\n".join(rows)
            text = f"📜 **Log**\n```{body}```"
        else:
            text = "📜 **Log**\n*(empty)*"

        # haal (of maak) het summary-bericht
        msg = None
        if st.thread_summary_message_id:
            try:
                msg = await th.fetch_message(st.thread_summary_message_id)
            except Exception:
                msg = None
        if msg is None:
            try:
                msg = await th.send(text)
                st.thread_summary_message_id = msg.id
                return
            except Exception:
                return
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
        if PANEL_CHANNEL_ID:
            ch = bot.get_channel(PANEL_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                st = state_for(ch.id)
                await ensure_panel(st)
                await ensure_log_thread(st)
                await update_log_summary(st)
                log.info("Panel ensured in channel %s", PANEL_CHANNEL_ID)

    @bot.event
    async def on_message(message: discord.Message):
        # zorg dat het paneel onderaan blijft
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if PANEL_CHANNEL_ID and message.channel.id != PANEL_CHANNEL_ID:
            return
        st = state_for(message.channel.id)
        try:
            async with st.lock:
                await send_panel_bottom(st)
        except Exception as e:
            log.warning("Move panel failed: %r", e)

    @bot.event
    async def on_interaction(interaction: Interaction):
        # knoppen afhandelen
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        st = state_for(ch.id)

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
                await update_log_summary(st)  # update thread
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

    # ----- command -----
    @bot.command()
    async def panel(ctx: commands.Context):
        st = state_for(ctx.channel.id)
        await ensure_panel(st)
        await ensure_log_thread(st)
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