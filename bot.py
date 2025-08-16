# bot.py — RushSearchBot: één panel + onbeperkt log in thread (met reset-logging & !clearpanel)
import os
import asyncio
import logging
from typing import List, Optional

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

LOG_THREAD_NAME = (os.getenv("LOG_THREAD_NAME", "log").strip() or "log")

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

            # log thread
            self.log_thread_id: Optional[int] = None
            self.log_header_message_id: Optional[int] = None  # optionele kop

            self.lock = asyncio.Lock()

        def panel_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.panel_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

        async def fetch_log_thread(self) -> Optional[discord.Thread]:
            """Zoek of maak de publieke thread met LOG_THREAD_NAME in het panel-kanaal."""
            if self.log_thread_id:
                th = bot.get_channel(self.log_thread_id)
                if isinstance(th, discord.Thread):
                    return th
                self.log_thread_id = None

            ch = self.panel_channel()
            if not ch:
                return None

            # open threads
            try:
                async for th in ch.threads(limit=50):
                    if th.name.lower() == LOG_THREAD_NAME.lower():
                        self.log_thread_id = th.id
                        return th
            except Exception:
                pass
            # archived threads
            try:
                async for th in ch.archived_threads(limit=50):
                    if th.name.lower() == LOG_THREAD_NAME.lower():
                        await th.unarchive()
                        self.log_thread_id = th.id
                        return th
            except Exception:
                pass

            # aanmaken
            try:
                th = await ch.create_thread(name=LOG_THREAD_NAME, type=discord.ChannelType.public_thread)
                self.log_thread_id = th.id
                log.info("Created log thread #%s in channel %s", LOG_THREAD_NAME, ch.id)
                return th
            except Exception as e:
                log.error("Failed to create/fetch log thread: %r", e)
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
            return interaction.channel and interaction.channel.id == self.channel_id

    # ----- helpers: panel -----
    def panel_text(st: PanelState) -> str:
        lines: List[str] = []

        # Searching — toon lokale tijd via Discord timestamp
        if st.current_user_id:
            if st.current_started_ts:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}> — <t:{st.current_started_ts}:t>")
            else:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")

        # lege regel
        lines.append("")

        # Queue — elke user op nieuwe regel
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def delete_other_panels(st: PanelState):
        """Zorg dat er maar één panel zichtbaar blijft."""
        ch = st.panel_channel()
        if not ch:
            return
        try:
            to_delete: List[discord.Message] = []
            async for m in ch.history(limit=50):
                if not m.author.bot:
                    continue
                if m.id == st.panel_message_id:
                    continue
                # herken panelberichten via knoppen of content
                if m.components:
                    for row in m.components:
                        for comp in getattr(row, "children", []):
                            if isinstance(comp, discord.Button) and str(comp.custom_id).startswith("rsb_"):
                                to_delete.append(m); break
                if m.content.startswith("🟦 **Searching**") or m.content.startswith("🔎 **Searching**"):
                    to_delete.append(m)
            for msg in to_delete:
                try: await msg.delete()
                except Exception: pass
        except Exception as e:
            log.warning("delete_other_panels failed: %r", e)

    async def send_panel_bottom(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {st.panel_channel_id} not found")
        # verwijder vorige panel als we het ID kennen
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(st), view=SearchView(st.panel_channel_id))
        st.panel_message_id = msg.id
        await delete_other_panels(st)
        await ensure_log_thread_and_header(st)
        return msg

    async def ensure_panel(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.panel_channel_id))
                await delete_other_panels(st)
                await ensure_log_thread_and_header(st)
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        try:
            await inter.response.edit_message(content=panel_text(st), view=SearchView(st.panel_channel_id))
        except discord.NotFound:
            await send_panel_bottom(st)
            try: await inter.response.defer()
            except Exception: pass
        except Exception:
            try: await inter.response.defer()
            except Exception: pass

    # ----- helpers: log-thread -----
    async def ensure_log_thread_and_header(st: PanelState):
        """Zorg dat de log-thread bestaat en plaats (optionele) kopregel."""
        thread = await st.fetch_log_thread()
        if not thread:
            return
        if st.log_header_message_id:
            try:
                await thread.fetch_message(st.log_header_message_id)
                return
            except Exception:
                st.log_header_message_id = None
        try:
            m = await thread.send("📜 **Log**")
            st.log_header_message_id = m.id
            try: await m.pin()
            except Exception: pass
        except Exception as e:
            log.warning("Couldn't send log header: %r", e)

    async def append_found_log(st: PanelState, user_id: int, ts_unix: int):
        thread = await st.fetch_log_thread()
        if not thread:
            await ensure_log_thread_and_header(st)
            thread = await st.fetch_log_thread()
            if not thread: return
        try:
            await thread.send(f"• <@{user_id}> — <t:{ts_unix}:t>")
        except Exception as e:
            log.warning("append_found_log failed: %r", e)

    async def append_reset_log(st: PanelState, target_user_id: int, by_user_id: int, ts_unix: int):
        thread = await st.fetch_log_thread()
        if not thread:
            await ensure_log_thread_and_header(st)
            thread = await st.fetch_log_thread()
            if not thread: return
        try:
            await thread.send(f"• <@{target_user_id}> ❌ by <@{by_user_id}> <t:{ts_unix}:t>")
        except Exception as e:
            log.warning("append_reset_log failed: %r", e)

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

    async def clear_all(st: PanelState):
        st.current_user_id = None
        st.current_started_ts = None
        st.queue = []
        # verwijder bekend panel
        ch = st.panel_channel()
        if ch and st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.delete()
            except Exception:
                pass
        st.panel_message_id = None
        await send_panel_bottom(st)

    # ----- events -----
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        bot.add_view(SearchView(PANEL_CHANNEL_ID))
        await ensure_panel(ST)
        await ensure_log_thread_and_header(ST)
        log.info("Panel ensured in channel %s; log thread ensured: '%s'", PANEL_CHANNEL_ID, LOG_THREAD_NAME)

    @bot.event
    async def on_message(message: discord.Message):
        # panel onderaan houden
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
        cid = (interaction.data or {}).get("custom_id")
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
                await append_found_log(st, user.id, ts)
                await stop_only(st)
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
                ts = int(discord.utils.utcnow().timestamp())
                await append_reset_log(st, st.current_user_id, inter.user.id, ts)
                await stop_only(st)  # geen auto-start
            await edit_panel_from_interaction(inter, st)

    # ----- commands -----
    @bot.command(name="clearpanel")
    @commands.guild_only()
    async def clearpanel(ctx: commands.Context):
        """Wis de huidige status en plaats een nieuw leeg panel (alleen in paneel-kanaal)."""
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id != PANEL_CHANNEL_ID:
            return
        # permissies: Manage Messages of Administrator
        perms = ctx.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            try:
                await ctx.reply("You need **Manage Messages** to use this.", delete_after=5)
            except Exception:
                pass
            return

        async with ST.lock:
            await clear_all(ST)
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @bot.command()
    async def panel(ctx: commands.Context):
        """Handmatig panel opnieuw renderen (alleen panel-kanaal)."""
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id != PANEL_CHANNEL_ID:
            return
        async with ST.lock:
            await ensure_panel(ST)
            await ensure_log_thread_and_header(ST)
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