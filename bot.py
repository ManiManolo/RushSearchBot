# bot.py — RushSearchBot: single panel + "log" thread reuse, unlimited log

import os
import asyncio
import logging
from typing import List, Optional, Dict

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction

# -------------------- logging --------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

# -------------------- env --------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))
if not PANEL_CHANNEL_ID:
    raise RuntimeError("PANEL_CHANNEL_ID missing")


# ==================== Bot factory ====================
def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    # ---------- state ----------
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
            if self.log_thread_id is None:
                return None
            th = bot.get_channel(self.log_thread_id)
            return th if isinstance(th, discord.Thread) else None

    ST = PanelState(PANEL_CHANNEL_ID)

    # -------------------- UI --------------------
    class SearchView(ui.View):
        def __init__(self, channel_id: int):
            super().__init__(timeout=None)
            self.channel_id = channel_id
            self.add_item(ui.Button(label="🔵 Search", style=ButtonStyle.primary,  custom_id="rsb_search"))
            self.add_item(ui.Button(label="✅ Found",  style=ButtonStyle.success,  custom_id="rsb_found"))
            self.add_item(ui.Button(label="🟡 Next",   style=ButtonStyle.secondary, custom_id="rsb_next"))
            self.add_item(ui.Button(label="🔁 Reset",  style=ButtonStyle.danger,    custom_id="rsb_reset"))

        async def interaction_check(self, interaction: Interaction) -> bool:
            # Alleen kliks in het paneelkanaal accepteren
            return interaction.channel and interaction.channel.id == self.channel_id

    # -------------------- panel helpers --------------------
    def panel_text() -> str:
        lines: List[str] = []

        # Searching (alleen tijd, geen "x minutes ago")
        if ST.current_user_id:
            if ST.current_started_ts:
                lines.append(f"🔎 **Searching**: <@{ST.current_user_id}> — <t:{ST.current_started_ts}:t>")
            else:
                lines.append(f"🔎 **Searching**: <@{ST.current_user_id}>")
        else:
            lines.append("🟦 **Searching**: *nobody*")

        # lege regel
        lines.append("")

        # Queue elk op nieuwe regel
        if ST.queue:
            queue_lines = "\n".join(f"• <@{uid}>" for uid in ST.queue)
            lines.append(f"🟡 **Queue**:\n{queue_lines}")
        else:
            lines.append("🟡 **Queue**:\n*empty*")

        return "\n".join(lines)

    async def send_panel_bottom() -> discord.Message:
        ch = ST.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {ST.panel_channel_id} not found")

        # verwijder vorig panel indien bekend
        if ST.panel_message_id:
            try:
                old = await ch.fetch_message(ST.panel_message_id)
                await old.delete()
            except Exception:
                pass

        msg = await ch.send(panel_text(), view=SearchView(ST.panel_channel_id))
        ST.panel_message_id = msg.id
        return msg

    async def ensure_single_panel() -> None:
        """Zorg dat er maar één paneel is: verwijder oude bot-berichten in dit kanaal en post één panel."""
        ch = ST.panel_channel()
        if not ch:
            return

        # verwijder recente bot-berichten (veilig tegen rate limits)
        try:
            async for m in ch.history(limit=50):
                if m.author == bot.user:
                    try:
                        await m.delete()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("Purge bot messages failed: %r", e)

        await send_panel_bottom()

    async def edit_panel_from_inter(inter: Interaction) -> None:
        # Probeer te editen; als dat niet kan, post onderaan opnieuw.
        try:
            await inter.response.edit_message(content=panel_text(), view=SearchView(ST.panel_channel_id))
        except discord.NotFound:
            await send_panel_bottom()
            try:
                await inter.response.defer()
            except Exception:
                pass
        except discord.InteractionResponded:
            # al beantwoord; proberen rechtstreeks bericht te bewerken
            try:
                ch = ST.panel_channel()
                if ch and ST.panel_message_id:
                    msg = await ch.fetch_message(ST.panel_message_id)
                    await msg.edit(content=panel_text(), view=SearchView(ST.panel_channel_id))
            except Exception:
                pass
        except Exception:
            try:
                await inter.response.defer()
            except Exception:
                pass

    # -------------------- log thread helpers --------------------
    async def get_or_create_log_thread() -> Optional[discord.Thread]:
        """Zoek (actief + gearchiveerd) naar thread met naam 'log'. Heropen indien nodig. Maak anders een nieuwe."""
        ch = ST.panel_channel()
        if not ch:
            return None

        # 1) zoek in actieve threads
        for th in ch.threads:
            if isinstance(th, discord.Thread) and th.name.lower() == "log":
                ST.log_thread_id = th.id
                # als 'ie per ongeluk locked is, ontgrendel
                try:
                    await th.edit(archived=False, locked=False)
                except Exception:
                    pass
                return th

        # 2) zoek in gearchiveerde public threads
        try:
            async for th in ch.archived_threads(limit=100, private=False):
                if isinstance(th, discord.Thread) and th.name.lower() == "log":
                    # heropen
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                    ST.log_thread_id = th.id
                    return th
        except Exception:
            # Sommige gateways/permissions laten archived fetch beperken; negeren.
            pass

        # 3) niets gevonden: nieuwe thread maken (public thread aan bericht onder)
        # We hebben een basisbericht nodig om een thread te starten.
        base = None
        if ST.panel_message_id:
            try:
                base = await ch.fetch_message(ST.panel_message_id)
            except Exception:
                base = None
        if base is None:
            base = await ch.send("Creating log thread…")  # tijdelijk
        try:
            th = await base.create_thread(name="log", auto_archive_duration=10080)  # 7 dagen
        except discord.HTTPException:
            # fallback direct via kanaal (sommige versies vereisen start_message)
            th = await ch.create_thread(name="log", auto_archive_duration=10080, type=discord.ChannelType.public_thread)

        ST.log_thread_id = th.id

        # ruim tijdelijk bericht op als we die net stuurden
        if base and base.content == "Creating log thread…":
            try:
                await base.delete()
            except Exception:
                pass

        return th

    async def append_log_line(text: str) -> None:
        """Plaats een regel in de log-thread (bestaat/wordt hergebruikt)."""
        th = ST.log_thread()
        if th is None:
            th = await get_or_create_log_thread()
        if th is None:
            log.warning("Log thread not available")
            return
        try:
            await th.send(text)
        except discord.Forbidden:
            log.warning("No permission to send in log thread")
        except Exception as e:
            log.warning("Sending to log thread failed: %r", e)

    # -------------------- state ops --------------------
    async def start_for(user_id: int) -> None:
        ST.current_user_id = user_id
        ST.current_started_ts = int(discord.utils.utcnow().timestamp())
        try:
            ST.queue.remove(user_id)
        except ValueError:
            pass

    async def stop_only() -> None:
        ST.current_user_id = None
        ST.current_started_ts = None

    # ==================== events ====================
    @bot.event
    async def on_ready():
        log.info("✅ Logged in as %s (id=%s)", getattr(bot.user, "name", "?"), getattr(bot.user, "id", "?"))
        # Zorg voor 1 paneel en hergebruik/maak log thread
        await ensure_single_panel()
        await get_or_create_log_thread()
        log.info("Panel ensured in channel %s; log thread ensured.", PANEL_CHANNEL_ID)

    @bot.event
    async def on_message(message: discord.Message):
        # Paneel onderaan houden: als iemand in het paneelkanaal praat, verplaats paneel naar onder
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.channel.id != PANEL_CHANNEL_ID:
            return

        try:
            async with ST.lock:
                await send_panel_bottom()
        except Exception as e:
            log.warning("Move panel failed: %r", e)

    @bot.event
    async def on_interaction(interaction: Interaction):
        # Alleen component-interacties (buttons)
        if interaction.type != discord.InteractionType.component:
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return
        if ch.id != PANEL_CHANNEL_ID:
            return

        cid = interaction.data.get("custom_id")
        if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
            return

        if cid == "rsb_search":
            await handle_search(interaction)
        elif cid == "rsb_found":
            await handle_found(interaction)
        elif cid == "rsb_next":
            await handle_next(interaction)
        elif cid == "rsb_reset":
            await handle_reset(interaction)

    # ==================== handlers ====================
    async def handle_search(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id is None:
                await start_for(user.id)
            else:
                # al iemand bezig: zet in queue als nog niet staat
                if user.id != ST.current_user_id and user.id not in ST.queue:
                    ST.queue.append(user.id)
            await edit_panel_from_inter(inter)

    async def handle_found(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id and ST.current_user_id == user.id:
                ts = int(discord.utils.utcnow().timestamp())
                # logregel (zonder codeblock, met mentions)
                await append_log_line(f"• {user.mention} ✅ <t:{ts}:t>")
                await stop_only()
            # paneel bijwerken
            await edit_panel_from_inter(inter)

    async def handle_next(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if user.id != ST.current_user_id and user.id not in ST.queue:
                ST.queue.append(user.id)
            await edit_panel_from_inter(inter)

    async def handle_reset(inter: Interaction):
        actor = inter.user  # wie klikt op reset
        async with ST.lock:
            if ST.current_user_id:
                target_id = ST.current_user_id
                await stop_only()  # géén auto-start
                ts = int(discord.utils.utcnow().timestamp())
                # log: wie is gereset en door wie
                await append_log_line(f"• <@{target_id}> ❌ by {actor.mention} <t:{ts}:t>")
            await edit_panel_from_inter(inter)

    # ==================== commands ====================
    @bot.command()
    async def clearpanel(ctx: commands.Context):
        """Maak paneel leeg (geen zoeker, geen queue). Log blijft behouden."""
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id != PANEL_CHANNEL_ID:
            return
        async with ST.lock:
            ST.current_user_id = None
            ST.current_started_ts = None
            ST.queue.clear()
            # we maken gewoon één nieuw paneel onderaan
            await send_panel_bottom()
        # verwijder het commandobericht om kanaal schoon te houden
        try:
            await ctx.message.delete()
        except Exception:
            pass

    return bot


# ==================== runner ====================
async def main():
    bot = make_bot()
    await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Shutting down...")