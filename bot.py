# bot.py — RushSearchBot (Background Worker, no pytz, free-start, collapsible log)
import os
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction

# ===== Logging =====
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

# ===== Environment =====
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in environment variables.")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))

# ===== Bot setup =====
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True  # nodig voor on_message
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ---------- Per-channel state ----------
    class PanelState:
        def __init__(self, channel_id: int):
            self.channel_id: int = channel_id
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # UNIX seconds
            self.queue: List[int] = []
            self.logbook: List[Tuple[int, int]] = []  # [(user_id, unix_ts)] max 20
            self.lock = asyncio.Lock()

        def channel(self) -> Optional[discord.TextChannel]:
            ch = bot.get_channel(self.channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None

    PANELS: Dict[int, PanelState] = {}

    def state_for(channel_id: int) -> PanelState:
        if channel_id not in PANELS:
            PANELS[channel_id] = PanelState(channel_id)
        return PANELS[channel_id]

    # ---------- UI ----------
    class SearchView(ui.View):
        def __init__(self, channel_id: int):
            super().__init__(timeout=None)  # persistent
            self.channel_id = channel_id
            self.add_item(ui.Button(label="🔵 Search", style=ButtonStyle.primary,  custom_id="rsb_search"))
            self.add_item(ui.Button(label="✅ Found",  style=ButtonStyle.success,  custom_id="rsb_found"))
            self.add_item(ui.Button(label="🟡 Next",   style=ButtonStyle.secondary, custom_id="rsb_next"))
            self.add_item(ui.Button(label="🔁 Reset",  style=ButtonStyle.danger,    custom_id="rsb_reset"))

        async def interaction_check(self, interaction: Interaction) -> bool:
            return interaction.channel and interaction.channel.id == self.channel_id

    # ---------- Panel helpers ----------
    def panel_text(st: PanelState) -> str:
        lines: List[str] = []

        # Searching
        if st.current_user_id:
            if st.current_started_ts:
                lines.append(
                    f"🔎 **Searching**: <@{st.current_user_id}> — since <t:{st.current_started_ts}:t>"
                )
            else:
                lines.append(f"🔎 **Searching**: <@{st.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")

        # Lege regel tussen Searching en Queue
        lines.append("")

        # Queue (elke user op nieuwe regel)
        if st.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in st.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        # Lege regel tussen Queue en Logboek
        lines.append("")

        # Logboek (laatste 20, spoiler = inklapbaar)
        if st.logbook:
            # nieuwste bovenaan
            last = st.logbook[-20:][::-1]
            log_lines = "\n".join(f"• <@{uid}> — <t:{ts}:t>" for uid, ts in last)
            lines.append(f"📜 **Log** (last 20):\n||{log_lines}||")
        else:
            lines.append("📜 **Log** (last 20):\n||*empty*||")

        return "\n".join(lines)

    async def send_panel_bottom(st: PanelState):
        """Post het panel onderaan; verwijder oud paneel als dat er is."""
        ch = st.channel()
        if not ch:
            raise RuntimeError(f"Channel {st.channel_id} not found or access denied.")
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(st), view=SearchView(st.channel_id))
        st.panel_message_id = msg.id
        return msg

    async def ensure_panel(st: PanelState):
        """Als panel bestaat: edit; anders: nieuw plaatsen onderaan."""
        ch = st.channel()
        if not ch:
            return
        if st.panel_message_id:
            try:
                msg = await ch.fetch_message(st.panel_message_id)
                await msg.edit(content=panel_text(st), view=SearchView(st.channel_id))
                return
            except Exception:
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        """Updaten via interaction; fallback = repost onderaan."""
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

    # ---------- State ops ----------
    async def start_for(st: PanelState, user_id: int):
        # Vrij starten: iedereen mag starten als het vrij is
        st.current_user_id = user_id
        st.current_started_ts = int(discord.utils.utcnow().timestamp())
        # eventueel uit queue halen zodat geen duplicaten
        try:
            st.queue.remove(user_id)
        except ValueError:
            pass

    async def stop_only(st: PanelState):
        # Reset zonder auto-start
        st.current_user_id = None
        st.current_started_ts = None

    # ---------- Events ----------
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
    async def on_message(message: discord.Message):
        # Panel onderaan houden: bij elk mens-bericht in panel-kanaal, repost panel
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
            log.warning("Failed to move panel to bottom: %r", e)

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

    # ---------- Handlers ----------
    async def handle_search(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if st.current_user_id is None:
                # vrij starten
                await start_for(st, user.id)
            else:
                # al bezig → enkel in de lijst zetten als hij er nog niet in staat
                if user.id != st.current_user_id and user.id not in st.queue:
                    st.queue.append(user.id)
            await edit_panel_from_interaction(inter, st)

    async def handle_found(inter: Interaction, st: PanelState):
        user = inter.user
        async with st.lock:
            if st.current_user_id and st.current_user_id == user.id:
                # log found
                ts = int(discord.utils.utcnow().timestamp())
                st.logbook.append((user.id, ts))
                if len(st.logbook) > 20:
                    st.logbook = st.logbook[-20:]
                # stop zonder auto-start
                await stop_only(st)
            # anders: geen actie
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
                # alleen huidige zoeker stoppen; niet automatisch door
                await stop_only(st)
            await edit_panel_from_interaction(inter, st)

    # ---------- Commands (stil) ----------
    @bot.command()
    async def panel(ctx: commands.Context):
        st = state_for(ctx.channel.id)
        await ensure_panel(st)
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