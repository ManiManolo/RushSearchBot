# bot.py — RushSearchBot: één panel + log in aparte thread (laatste 50)
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

LOG_THREAD_NAME = os.getenv("LOG_THREAD_NAME", "log").strip() or "log"

# ---------- constants ----------
LOG_LIMIT = 50  # laatste 50 in log

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

            # log (samenvattingsbericht in een losse thread)
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
                self.log_thread_id = None
            # Zoeken op naam
            ch = self.panel_channel()
            if not ch:
                return None
            try:
                async for th in ch.threads(limit=50):
                    if th.name.lower() == LOG_THREAD_NAME.lower():
                        self.log_thread_id = th.id
                        return th
            except Exception:
                pass
            try:
                async for th in ch.archived_threads(limit=50):
                    if th.name.lower() == LOG_THREAD_NAME.lower():
                        await th.unarchive()
                        self.log_thread_id = th.id
                        return th
            except Exception:
                pass
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
            # Alleen knoppen in het panel-kanaal
            return interaction.channel and interaction.channel.id == self.channel_id

    # ----- helpers: panel rendering -----
    def panel_text(st: PanelState) -> str:
        lines: List[str] = []

        # Searching — toon alleen tijd (lokale tijd per kijker via Discord timestamp)
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

    async def delete_other_panels(st: PanelState):
        """Houd slechts 1 panel; verwijder oudere bot-panels in dit kanaal."""
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
                # Als een bot-bericht onze knoppen bevat: weg ermee
                if m.components:
                    for row in m.components:
                        for comp in getattr(row, "children", []):
                            if isinstance(comp, discord.Button) and str(comp.custom_id).startswith("rsb_"):
                                to_delete.append(m)
                                break
                # Val-back: herken aan header
                if m.content.startswith("🟦 **Searching**") or m.content.startswith("🔎 **Searching**"):
                    to_delete.append(m)
            if to_delete:
                for msg in to_delete:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("delete_other_panels failed: %r", e)

    async def send_panel_bottom(st: PanelState):
        ch = st.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {st.panel_channel_id} not found")
        # verwijder huidig panel als-ie bestaat
        if st.panel_message_id:
            try:
                old = await ch.fetch_message(st.panel_message_id)
                await old.delete()
            except Exception:
                pass
        # nieuw panel onderaan
        msg = await ch.send(panel_text(st), view=SearchView(st.panel_channel_id))
        st.panel_message_id = msg.id
        await delete_other_panels(st)
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
                await delete_other_panels(st)
                await ensure_log_thread_and_message(st)
                return
            except Exception:
                # als niet te vinden: maak ‘m opnieuw
                pass
        await send_panel_bottom(st)

    async def edit_panel_from_interaction(inter: Interaction, st: PanelState):
        # probeer te editen, lukt dat niet -> onderaan opnieuw plaatsen
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

    # ----- helpers: log thread -----
    def fmt_hhmm(ts_unix: int) -> str:
        # Discord gebruikt viewer-lokale tijd als we <t:..:t> gebruiken,
        # maar in de log willen we vaste HH:MM tekst; neem UTC-agnostisch simpel:
        dt = discord.utils.utcnow().fromtimestamp(ts_unix, tz=None)  # type: ignore
        # bovenstaande geeft naïef object; pak HH:MM van UTC; we willen alleen tijdnotatie
        return dt.strftime("%H:%M")

    async def ensure_log_thread_and_message(st: PanelState):
        """Zorg dat de thread 'log' bestaat en er één samenvattingsbericht in staat."""
        ch = st.panel_channel()
        if not ch:
            return

        thread = await st.fetch_log_thread()
        if not thread:
            try:
                thread = await ch.create_thread(name=LOG_THREAD_NAME, type=discord.ChannelType.public_thread)
                st.log_thread_id = thread.id
                log.info("Created log thread #%s in channel %s", LOG_THREAD_NAME, ch.id)
            except discord.Forbidden:
                log.error("No permission to create thread in channel %s", ch.id)
                return
            except Exception as e:
                log.error("Failed to create thread: %r", e)
                return

        # check of log-message er is
        if st.log_message_id:
            try:
                await thread.fetch_message(st.log_message_id)
                return
            except Exception:
                st.log_message_id = None
        try:
            m = await thread.send("📜 **Log**\n*(empty)*")
            st.log_message_id = m.id
        except Exception as e:
            log.warning("Couldn't send log summary: %r", e)

    async def update_log_summary(st: PanelState):
        thread = await st.fetch_log_thread()
        if not thread:
            await ensure_log_thread_and_message(st)
            thread = await st.fetch_log_thread()
            if not thread:
                return

        await ensure_log_thread_and_message(st)

        try:
            msg = await thread.fetch_message(st.log_message_id)  # type: ignore[arg-type]
        except Exception:
            m2 = await thread.send("📜 **Log**\n*(empty)*")
            st.log_message_id = m2.id
            msg = m2

        last = st.logbook[-LOG_LIMIT:][::-1]  # nieuwste boven
        if last:
            # geen code block -> mentions blijven klikbaar
            rows = [f"• <@{uid}> — {fmt_hhmm(ts)}" for uid, ts in last]
            text = "📜 **Log**\n" + "\n".join(rows)
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
        # persistent view is niet strikt nodig omdat we on_interaction gebruiken,
        # maar toevoegen kan geen kwaad als bots later herstarten met bestaand panel:
        bot.add_view(SearchView(PANEL_CHANNEL_ID))
        await ensure_panel(ST)
        await ensure_log_thread_and_message(ST)
        await update_log_summary(ST)
        log.info("Panel ensured in channel %s; log thread ensured: '%s'", PANEL_CHANNEL_ID, LOG_THREAD_NAME)

    @bot.event
    async def on_message(message: discord.Message):
        # panel onderaan houden in panel-kanaal
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
        cid = (interaction.data or {}).get("custom_id")  # type: ignore[assignment]
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        if ch.id != PANEL_CHANNEL_ID:
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
                # voeg toe aan wachtrij als nog niet erin en niet al aan het zoeken
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
                await stop_only(st)  # géén auto-start
            await edit_panel_from_interaction(inter, st)

    # ----- optional command -----
    @bot.command()
    async def panel(ctx: commands.Context):
        """Handmatig panel opnieuw plaatsen (alleen in panel-kanaal)."""
        if ctx.channel.id != PANEL_CHANNEL_ID:
            return
        await ensure_panel(ST)
        await ensure_log_thread_and_message(ST)
        await update_log_summary(ST)
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