# bot.py — RushSearchBot: panel + log-thread + clear command
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
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
LOG_THREAD_NAME = "log"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")
if not PANEL_CHANNEL_ID:
    raise RuntimeError("PANEL_CHANNEL_ID missing")

TZ = ZoneInfo("Europe/Amsterdam")

# ---------- bot ----------
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    class PanelState:
        def __init__(self, panel_channel_id: int):
            self.panel_channel_id: int = panel_channel_id
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None
            self.queue: List[int] = []
            self.log_thread_id: Optional[int] = None
            self.lock = asyncio.Lock()

        def panel_channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.panel_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

        def log_thread(self) -> Optional[discord.Thread]:
            ch = bot.get_channel(self.log_thread_id)
            return ch if isinstance(ch, discord.Thread) else None

    ST: PanelState = PanelState(PANEL_CHANNEL_ID)

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
    def panel_text(st: PanelState) -> str:
        lines: List[str] = []
        if st.current_user_id:
            if st.current_started_ts:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}> — <t:{st.current_started_ts}:t>")
            else:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")
        lines.append("")
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")
        return "\n".join(lines)

    async def send_panel_bottom(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError("Panel channel not found")
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(st), view=SearchView(st.panel_channel_id))
        st.panel_message_id = msg.id
        return msg

    async def ensure_panel(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.panel_channel_id))
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    # ---------- log helpers ----------
    async def ensure_log_thread(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            return
        if st.log_thread_id:
            try:
                th = await bot.fetch_channel(st.log_thread_id)
                if isinstance(th, discord.Thread):
                    return
            except Exception:
                st.log_thread_id = None
        async for thread in ch.threads:
            if thread.name.lower() == LOG_THREAD_NAME.lower():
                st.log_thread_id = thread.id
                return
        th = await ch.create_thread(name=LOG_THREAD_NAME, type=discord.ChannelType.public_thread)
        st.log_thread_id = th.id

    async def add_log(st: PanelState, text: str):
        await ensure_log_thread(st)
        th = st.log_thread()
        if th:
            await th.send(text, allowed_mentions=discord.AllowedMentions.none())

    # ---------- state ops ----------
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

    # ---------- events ----------
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s", bot.user)
        await ensure_panel(ST)
        await ensure_log_thread(ST)
        log.info("Panel and log ready")

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return
        if isinstance(message.channel, discord.TextChannel) and message.channel.id == PANEL_CHANNEL_ID:
            async with ST.lock:
                await send_panel_bottom(ST)

    @bot.event
    async def on_interaction(interaction: Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.channel.id != PANEL_CHANNEL_ID:
            return
        if cid == "rsb_search":
            await handle_search(interaction)
        elif cid == "rsb_found":
            await handle_found(interaction)
        elif cid == "rsb_next":
            await handle_next(interaction)
        elif cid == "rsb_reset":
            await handle_reset(interaction)

    # ---------- button handlers ----------
    async def handle_search(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id is None:
                await start_for(ST, user.id)
            elif user.id != ST.current_user_id and user.id not in ST.queue:
                ST.queue.append(user.id)
            await ensure_panel(ST)
            await inter.response.defer()

    async def handle_found(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id == user.id:
                await add_log(ST, f"• <@{user.id}> ✅ <t:{int(discord.utils.utcnow().timestamp())}:t>")
                await stop_only(ST)
            await ensure_panel(ST)
            await inter.response.defer()

    async def handle_next(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if user.id != ST.current_user_id and user.id not in ST.queue:
                ST.queue.append(user.id)
            await ensure_panel(ST)
            await inter.response.defer()

    async def handle_reset(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id:
                await add_log(
                    ST,
                    f"• <@{ST.current_user_id}> ❌ by <@{user.id}> <t:{int(discord.utils.utcnow().timestamp())}:t>"
                )
                await stop_only(ST)
            await ensure_panel(ST)
            await inter.response.defer()

    # ---------- commands ----------
    @bot.command(name="panel")
    async def cmd_panel(ctx: commands.Context):
        if ctx.channel.id != PANEL_CHANNEL_ID:
            return await ctx.message.delete()
        await ensure_panel(ST)
        await ctx.message.delete()

    @bot.command(name="clear")
    async def clear_user(ctx: commands.Context, member: Optional[discord.Member] = None):
        chan_ok = False
        if isinstance(ctx.channel, discord.TextChannel) and ctx.channel.id == PANEL_CHANNEL_ID:
            chan_ok = True
        elif isinstance(ctx.channel, discord.Thread) and getattr(ctx.channel, "parent_id", None) == PANEL_CHANNEL_ID:
            chan_ok = True
        if not chan_ok:
            return await ctx.message.delete()

        # Geen member meegegeven → probeer via reply
        if member is None and ctx.message.reference:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if isinstance(ref_msg.author, discord.Member):
                    member = ref_msg.author
            except Exception:
                pass

        if member is None:
            return await ctx.message.delete()

        actor = ctx.author
        changed = False
        async with ST.lock:
            if ST.current_user_id == member.id:
                await stop_only(ST)
                await add_log(ST, f"• <@{member.id}> ❌ cleared by <@{actor.id}> <t:{int(discord.utils.utcnow().timestamp())}:t>")
                changed = True
            if member.id in ST.queue:
                ST.queue = [u for u in ST.queue if u != member.id]
                await add_log(ST, f"• <@{member.id}> ❌ removed from queue by <@{actor.id}> <t:{int(discord.utils.utcnow().timestamp())}:t>")
                changed = True
            if changed:
                await ensure_panel(ST)

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