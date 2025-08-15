# bot.py — RushSearchBot: single panel + public log thread "log" (last 50)
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
if not PANEL_CHANNEL_ID:
    raise RuntimeError("PANEL_CHANNEL_ID missing")

LOG_THREAD_NAME = os.getenv("LOG_THREAD_NAME", "log").strip() or "log"

# ---------- constants ----------
LOG_LIMIT = 50
TZ = ZoneInfo("Europe/Amsterdam")  # HH:MM in log

# ---------- bot ----------
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ----- state -----
    class PanelState:
        def __init__(self, panel_channel_id: int):
            self.panel_channel_id: int = panel_channel_id

            # panel
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # unix seconds
            self.queue: List[int] = []

            # log (in public thread binnen dit kanaal)
            self.log_thread_id: Optional[int] = None
            self.log_message_id: Optional[int] = None
            self.logbook: List[Tuple[int, int]] = []  # (user_id, ts)

            self.lock = asyncio.Lock()

        def panel_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.panel_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

        async def fetch_log_thread(self) -> Optional[discord.Thread]:
            if self.log_thread_id:
                th = bot.get_channel(self.log_thread_id)
                if isinstance(th, discord.Thread):
                    return th
            return None

    ST = PanelState(PANEL_CHANNEL_ID)

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
            # Alleen klikken in het juiste kanaal
            return interaction.channel and interaction.channel.id == self.channel_id

    # ----- helpers: panel rendering -----
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

        # Queue (één per regel)
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def delete_old_panels(ch: discord.TextChannel, keep_id: Optional[int]):
        # verwijder ALLE bot-berichten in dit kanaal behalve het huidige paneel (keep_id)
        try:
            async for msg in ch.history(limit=50):
                if msg.author == bot.user and (keep_id is None or msg.id != keep_id):
                    # heuristiek: onze panelberichten hebben een View (knoppen) en beginnen met 🔎/🟦
                    if (msg.components or msg.embeds or msg.content.startswith(("🔎", "🟦"))):
                        with contextlib.suppress(Exception):
                            await msg.delete()
        except Exception as e:
            log.debug("delete_old_panels error: %r", e)

    async def send_panel_bottom(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {st.panel_channel_id} not found")

        # verwijder vorige paneel eerst (garandeert 1 zichtbaar paneel)
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass

        msg = await ch.send(panel_text(st), view=SearchView(st.panel_channel_id))
        st.panel_message_id = msg.id

        # opruimen van eventuele (hele) oude panelen
        await delete_old_panels(ch, keep_id=st.panel_message_id)

        # zorg dat log thread & bericht bestaan
        await ensure_log_thread_and_message(st)
        return msg

    async def ensure_panel(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.panel_channel_id))
                await ensure_log_thread_and_message(st)
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        # probeer te editen (snel), anders opnieuw onderaan posten
        try:
            await inter.response.edit_message(content=panel_text(st), view=SearchView(st.panel_channel_id))
        except discord.NotFound:
            await send_panel_bottom(st)
            with contextlib.suppress(Exception):
                await inter.response.defer()
        except Exception:
            with contextlib.suppress(Exception):
                await inter.response.defer()

    # ----- helpers: log thread -----
    import contextlib

    def fmt_hhmm(ts_unix: int) -> str:
        # Note: discord.ts → UNIX is UTC; hier tonen we EU/Amsterdam HH:MM
        dt = discord.utils.datetime.datetime.fromtimestamp(ts_unix, TZ)
        return dt.strftime("%H:%M")

    async def find_existing_log_thread(ch: discord.TextChannel) -> Optional[discord.Thread]:
        # 1) check actieve threads
        for th in ch.threads:
            if isinstance(th, discord.Thread) and th.name.lower() == LOG_THREAD_NAME.lower():
                return th
        # 2) check gearchiveerde public threads (max ~100)
        try:
            async for th in ch.archived_threads(limit=100, private=False):
                if th.name.lower() == LOG_THREAD_NAME.lower():
                    # unarchive door te sturen/bewerken is niet direct; we kunnen het gewoon gebruiken (auto-unarchive bij send)
                    return th
        except Exception:
            pass
        return None

    async def ensure_log_thread_and_message(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return

        # haal/bouw thread
        thread = await st.fetch_log_thread()
        if not thread:
            thread = await find_existing_log_thread(ch)

        if not thread:
            # maak public, auto-archive 7 dagen
            try:
                thread = await ch.create_thread(
                    name=LOG_THREAD_NAME,
                    auto_archive_duration=10080,  # 7d
                    type=discord.ChannelType.public_thread,
                )
                log.info("Created log thread '%s' (id=%s)", LOG_THREAD_NAME, getattr(thread, "id", "?"))
            except discord.Forbidden:
                log.error("Missing permissions to create thread in channel %s", ch.id)
                return
            except Exception as e:
                log.error("create_thread failed: %r", e)
                return

        st.log_thread_id = thread.id

        # zorg dat er een samenvattingsbericht is in de thread
        if st.log_message_id:
            with contextlib.suppress(Exception):
                await thread.fetch_message(st.log_message_id)
                return

        # Geen bestaand message-id? Zoek er één die met kopje begint:
        existing = None
        try:
            async for m in thread.history(limit=50, oldest_first=True):
                if m.author == bot.user and m.content.startswith("📜 **Log**"):
                    existing = m
                    break
        except Exception:
            pass

        if existing:
            st.log_message_id = existing.id
            return

        # nieuw samenvattingsbericht
        msg = await thread.send("📜 **Log**\n*(empty)*")
        st.log_message_id = msg.id

    async def update_log_summary(st: PanelState):
        thread = await st.fetch_log_thread()
        if not thread:
            await ensure_log_thread_and_message(st)
            thread = await st.fetch_log_thread()
            if not thread:
                return

        # zorg dat het samenvattingsbericht er is
        await ensure_log_thread_and_message(st)

        try:
            msg = await thread.fetch_message(st.log_message_id)
        except Exception:
            # opnieuw maken
            with contextlib.suppress(Exception):
                m2 = await thread.send("📜 **Log**\n*(empty)*")
                st.log_message_id = m2.id
                msg = m2

        last = st.logbook[-LOG_LIMIT:][::-1]  # nieuwste bovenaan
        if last:
            # compact tabelletje
            rows = [f"{('<@'+str(uid)+'>'):<22} {fmt_hhmm(ts)}" for uid, ts in last]
            body = "\n".join(rows)
            text = f"📜 **Log**\n```{body}```"
        else:
            text = "📜 **Log**\n*(empty)*"

        with contextlib.suppress(Exception):
            await msg.edit(content=text)

    # ----- state ops -----
    async def start_for(st: PanelState, user_id: int):
        st.current_user_id = user_id
        st.current_started_ts = int(discord.utils.utcnow().timestamp())
        with contextlib.suppress(ValueError):
            st.queue.remove(user_id)

    async def stop_only(st: PanelState):
        st.current_user_id = None
        st.current_started_ts = None

    # ----- events -----
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        # paneel & log-thread zeker stellen
        await ensure_panel(ST)
        await ensure_log_thread_and_message(ST)
        await update_log_summary(ST)
        log.info("Panel ensured in channel %s; Log thread name '%s'", PANEL_CHANNEL_ID, LOG_THREAD_NAME)

    @bot.event
    async def on_message(message: discord.Message):
        # paneel onderaan houden in het paneel-kanaal
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id != PANEL_CHANNEL_ID:
            return
        try:
            async with ST.lock:
                await send_panel_bottom(ST)
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
        if not isinstance(ch, discord.TextChannel) or ch.id != PANEL_CHANNEL_ID:
            return

        if cid == "rsb_search":
            await handle_search(interaction, ST)
        elif cid == "rsb_found":
            await handle_found(interaction, ST)
        elif cid == "rsb_next":
            await handle_next(interaction, ST)
        elif cid == "rsb_reset":
            await handle_reset(interaction, ST)

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
                await stop_only(st)  # geen auto-next!
                await update_log_summary(st)  # update in log-thread
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
                await stop_only(st)  # géén auto-start van volgende
            await edit_panel_from_interaction(inter, st)

    # ----- manual command (optioneel) -----
    @bot.command()
    async def panel(ctx: commands.Context):
        if ctx.channel.id != PANEL_CHANNEL_ID:
            return
        await ensure_panel(ST)
        await ensure_log_thread_and_message(ST)
        await update_log_summary(ST)
        with contextlib.suppress(Exception):
            await ctx.message.delete()

    return bot


async def main():
    bot = make_bot()
    await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Shutting down...")