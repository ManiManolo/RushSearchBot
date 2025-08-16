# bot.py — RushSearchBot: single panel + "log" thread + !clear @user
import os
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

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

# ---------- bot factory ----------
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    ALLOWED_NONE = discord.AllowedMentions.none()  # nooit pingen in panel/log

    # ----- state -----
    class PanelState:
        def __init__(self, panel_channel_id: int):
            self.panel_channel_id: int = panel_channel_id

            # panel
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # unix seconds
            self.queue: List[int] = []

            # log thread
            self.log_thread_id: Optional[int] = None

            self.lock = asyncio.Lock()

        def panel_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.panel_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

        def log_thread(self) -> Optional[discord.Thread]:
            if self.log_thread_id:
                th = bot.get_channel(self.log_thread_id)
                return th if isinstance(th, discord.Thread) else None
            return None

    st = PanelState(PANEL_CHANNEL_ID)

    # ---------- UI ----------
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

    # ---------- panel rendering ----------
    def panel_text() -> str:
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

        # Queue (elk op nieuwe regel)
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def send_panel_bottom():
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {st.panel_channel_id} not found")

        # verwijder vorig paneel (als we het id weten)
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass

        msg = await ch.send(panel_text(), view=SearchView(st.panel_channel_id), allowed_mentions=ALLOWED_NONE)
        st.panel_message_id = msg.id
        return msg

    async def ensure_single_panel():
        """Zoek recente panel-berichten van de bot en behoud er maar 1."""
        ch = st.panel_channel()
        if not ch:
            return
        candidates: List[discord.Message] = []
        try:
            async for m in ch.history(limit=50):
                if m.author.id == bot.user.id and isinstance(m.components, list):
                    # ruwe check: onze panel-tekst bevat 'Searching' en 'Queue' en heeft knoppen
                    if "Searching" in m.content and "Queue" in m.content and m.components:
                        candidates.append(m)
        except Exception:
            pass

        candidates.sort(key=lambda m: m.created_at)  # oud -> nieuw
        if candidates:
            # houd enkel de nieuwste
            latest = candidates[-1]
            for extra in candidates[:-1]:
                try:
                    await extra.delete()
                except Exception:
                    pass
            st.panel_message_id = latest.id
            try:
                await latest.edit(content=panel_text(), view=SearchView(st.panel_channel_id), allowed_mentions=ALLOWED_NONE)
            except Exception:
                # als editen niet kan, maak een nieuwe onderaan
                await send_panel_bottom()
        else:
            await send_panel_bottom()

    async def edit_panel_from_interaction(inter: Interaction):
        # probeer te editen, anders opnieuw onderaan
        try:
            await inter.response.edit_message(content=panel_text(), view=SearchView(st.panel_channel_id), allowed_mentions=ALLOWED_NONE)
        except discord.NotFound:
            await send_panel_bottom()
            try:
                await inter.response.defer()
            except Exception:
                pass
        except Exception:
            try:
                await inter.response.defer()
            except Exception:
                pass

    # ---------- log thread ----------
    async def ensure_log_thread() -> Optional[discord.Thread]:
        """Zoek of maak een thread 'log' onder het panel-kanaal, en hergebruik die ook na herstart."""
        ch = st.panel_channel()
        if not ch:
            return None

        # 1) als we een id hebben, probeer op te halen
        if st.log_thread_id:
            th = st.log_thread()
            if th:
                return th
            else:
                st.log_thread_id = None  # ongeldig geworden

        # 2) kijk actieve threads in dit kanaal
        for th in ch.threads:
            if isinstance(th, discord.Thread) and th.name.lower() == "log":
                st.log_thread_id = th.id
                # als gearchiveerd, unarchive
                if th.archived:
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                return th

        # 3) kijk gearchiveerde public threads
        try:
            archived = []
            async for th in ch.archived_threads(limit=100, private=False):
                archived.append(th)
            for th in archived:
                if th.name.lower() == "log":
                    st.log_thread_id = th.id
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                    return th
        except Exception:
            pass

        # 4) bestaat niet: maak nieuwe thread
        # We hechten de thread aan het huidige (of nieuwe) panel-bericht
        base_msg: Optional[discord.Message] = None
        if st.panel_message_id:
            try:
                base_msg = await ch.fetch_message(st.panel_message_id)
            except Exception:
                base_msg = None
        if base_msg is None:
            base_msg = await send_panel_bottom()

        try:
            th = await base_msg.create_thread(name="log", auto_archive_duration=10080)  # 7 dagen
            st.log_thread_id = th.id
            return th
        except Exception as e:
            log.warning("Failed to create/find log thread: %r", e)
            return None

    async def log_line(text: str):
        th = await ensure_log_thread()
        if not th:
            return
        try:
            await th.send(text, allowed_mentions=ALLOWED_NONE)
        except Exception:
            pass

    def ts_now() -> int:
        return int(discord.utils.utcnow().timestamp())

    # ---------- state ops ----------
    async def start_for(user_id: int):
        st.current_user_id = user_id
        st.current_started_ts = ts_now()
        try:
            st.queue.remove(user_id)
        except ValueError:
            pass

    async def stop_only():
        st.current_user_id = None
        st.current_started_ts = None

    # ---------- events ----------
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        # paneel zeker stellen en één houden
        await ensure_single_panel()
        # log thread zeker stellen
        await ensure_log_thread()
        log.info("Panel ensured in channel %s; log thread ensured (name='log')", PANEL_CHANNEL_ID)

    @bot.event
    async def on_message(message: discord.Message):
        # paneel onderaan houden in het paneel-kanaal
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id != PANEL_CHANNEL_ID:
            return
        async with st.lock:
            await send_panel_bottom()

    @bot.event
    async def on_interaction(interaction: Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        data = getattr(interaction, "data", {}) or {}
        cid = data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or ch.id != PANEL_CHANNEL_ID:
            return

        if cid == "rsb_search":
            await handle_search(interaction)
        elif cid == "rsb_found":
            await handle_found(interaction)
        elif cid == "rsb_next":
            await handle_next(interaction)
        elif cid == "rsb_reset":
            await handle_reset(interaction)

    # ---------- handlers ----------
    async def handle_search(inter: Interaction):
        user = inter.user
        async with st.lock:
            if st.current_user_id is None:
                await start_for(user.id)
            else:
                if user.id != st.current_user_id and user.id not in st.queue:
                    st.queue.append(user.id)
            await edit_panel_from_interaction(inter)

    async def handle_found(inter: Interaction):
        user = inter.user
        async with st.lock:
            if st.current_user_id and st.current_user_id == user.id:
                ts = ts_now()
                await log_line(f"• <@{user.id}> ✅ <t:{ts}:t>")
                await stop_only()  # geen auto-start
            await edit_panel_from_interaction(inter)

    async def handle_next(inter: Interaction):
        user = inter.user
        async with st.lock:
            if user.id != st.current_user_id and user.id not in st.queue:
                st.queue.append(user.id)
            await edit_panel_from_interaction(inter)

    async def handle_reset(inter: Interaction):
        actor = inter.user
        async with st.lock:
            if st.current_user_id:
                target = st.current_user_id
                await stop_only()  # geen auto-start
                ts = ts_now()
                await log_line(f"• <@{target}> ❌ by <@{actor.id}> <t:{ts}:t>")
            await edit_panel_from_interaction(inter)

    # ---------- commands ----------
    @bot.command()
    async def panel(ctx: commands.Context):
        """Zet (opnieuw) precies één panel onderaan het kanaal."""
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id != PANEL_CHANNEL_ID:
            return
        async with st.lock:
            await ensure_single_panel()
            await ensure_log_thread()
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @bot.command(name="clear")
    async def clear_user(ctx: commands.Context, member: Optional[discord.Member] = None):
        """
        Verwijder specifiek iemand uit de queue of stop de huidige zoeker.
        Gebruik: !clear @user
        """
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id != PANEL_CHANNEL_ID:
            return
        if member is None:
            # geen user meegegeven; niets doen (bewust stil)
            try:
                await ctx.message.delete()
            except Exception:
                pass
            return

        actor = ctx.author
        target_id = member.id

        async with st.lock:
            changed = False
            # als target nu zoekt -> stop
            if st.current_user_id == target_id:
                await stop_only()
                ts = ts_now()
                await log_line(f"• <@{target_id}> ❌ by <@{actor.id}> <t:{ts}:t>")
                changed = True
            # verwijder uit queue
            if target_id in st.queue:
                st.queue = [uid for uid in st.queue if uid != target_id]
                ts = ts_now()
                await log_line(f"• <@{target_id}> ❌ by <@{actor.id}> <t:{ts}:t>")
                changed = True

            if changed:
                # paneel bijwerken
                ch = st.panel_channel()
                if ch and st.panel_message_id:
                    try:
                        msg = await ch.fetch_message(st.panel_message_id)
                        await msg.edit(content=panel_text(), view=SearchView(st.panel_channel_id), allowed_mentions=ALLOWED_NONE)
                    except Exception:
                        await send_panel_bottom()

        # verwijder het command-bericht voor clean kanaal
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