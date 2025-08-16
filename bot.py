# bot.py — RushSearchBot: single panel, log thread, no pings, + !clear @user

import os
import asyncio
import logging
from typing import List, Optional

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

# Mentions uitzetten (weergave blijft, maar zonder notificaties)
NO_PINGS = discord.AllowedMentions.none()


def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None, allowed_mentions=NO_PINGS)

    # ---------- state ----------
    class PanelState:
        def __init__(self, panel_channel_id: int):
            self.panel_channel_id: int = panel_channel_id
            self.panel_message_id: Optional[int] = None
            self.current_user_id: Optional[int] = None
            self.current_started_ts: Optional[int] = None  # unix seconds
            self.queue: List[int] = []
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

        def clear(self):
            self.current_user_id = None
            self.current_started_ts = None
            self.queue.clear()

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
            return interaction.channel and interaction.channel.id == self.channel_id

    # -------------------- panel helpers --------------------
    def panel_text() -> str:
        lines: List[str] = []
        # Searching (alleen tijd)
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

    async def delete_old_panels(ch: discord.TextChannel, limit: int = 200) -> None:
        """Verwijder recente bot-berichten (oude panels) in het paneelkanaal."""
        try:
            async for m in ch.history(limit=limit, oldest_first=False):
                if m.author == bot.user:
                    try:
                        await m.delete()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("Purge bot messages failed: %r", e)

    async def send_panel_bottom() -> discord.Message:
        ch = ST.panel_channel()
        if not ch:
            raise RuntimeError(f"Panel channel {ST.panel_channel_id} not found")
        if ST.panel_message_id:
            try:
                old = await ch.fetch_message(ST.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await ch.send(panel_text(), view=SearchView(ST.panel_channel_id), allowed_mentions=NO_PINGS)
        ST.panel_message_id = msg.id
        return msg

    async def render_panel_in_place() -> None:
        """Probeer het huidige panel te overschrijven; val terug op ensure_single_panel()."""
        ch = ST.panel_channel()
        if not ch:
            return
        if ST.panel_message_id:
            try:
                msg = await ch.fetch_message(ST.panel_message_id)
                await msg.edit(content=panel_text(), view=SearchView(ST.panel_channel_id), allowed_mentions=NO_PINGS)
                return
            except Exception:
                pass
        await ensure_single_panel()

    async def ensure_single_panel() -> None:
        """Zorg dat er maar één paneel staat: wis bot-berichten en post opnieuw."""
        ch = ST.panel_channel()
        if not ch:
            return
        await delete_old_panels(ch, limit=200)
        await send_panel_bottom()

    async def edit_panel_from_inter(inter: Interaction) -> None:
        try:
            await inter.response.edit_message(
                content=panel_text(),
                view=SearchView(ST.panel_channel_id),
                allowed_mentions=NO_PINGS,
            )
        except discord.NotFound:
            await send_panel_bottom()
            try:
                await inter.response.defer()
            except Exception:
                pass
        except discord.InteractionResponded:
            try:
                ch = ST.panel_channel()
                if ch and ST.panel_message_id:
                    msg = await ch.fetch_message(ST.panel_message_id)
                    await msg.edit(content=panel_text(), view=SearchView(ST.panel_channel_id), allowed_mentions=NO_PINGS)
            except Exception:
                pass
        except Exception:
            try:
                await inter.response.defer()
            except Exception:
                pass

    # -------------------- log thread helpers --------------------
    async def get_or_create_log_thread() -> Optional[discord.Thread]:
        """Zoek (actief + gearchiveerd) naar thread met naam 'log'. Anders maken."""
        ch = ST.panel_channel()
        if not ch:
            return None

        # actieve threads
        for th in ch.threads:
            if isinstance(th, discord.Thread) and th.name.lower() == "log":
                ST.log_thread_id = th.id
                try:
                    await th.edit(archived=False, locked=False)
                except Exception:
                    pass
                return th

        # gearchiveerde public threads
        try:
            async for th in ch.archived_threads(limit=100, private=False):
                if isinstance(th, discord.Thread) and th.name.lower() == "log":
                    try:
                        await th.edit(archived=False, locked=False)
                    except Exception:
                        pass
                    ST.log_thread_id = th.id
                    return th
        except Exception:
            pass

        # niets gevonden -> nieuwe thread
        base = None
        if ST.panel_message_id:
            try:
                base = await ch.fetch_message(ST.panel_message_id)
            except Exception:
                base = None
        if base is None:
            base = await ch.send("Creating log thread…", allowed_mentions=NO_PINGS)
        try:
            th = await base.create_thread(name="log", auto_archive_duration=10080)
        except discord.HTTPException:
            th = await ch.create_thread(
                name="log",
                auto_archive_duration=10080,
                type=discord.ChannelType.public_thread,
            )

        ST.log_thread_id = th.id

        if base and base.content == "Creating log thread…":
            try:
                await base.delete()
            except Exception:
                pass

        return th

    async def append_log_line(text: str) -> None:
        """Plaats een regel in de log-thread, zonder mensen te pingen."""
        th = ST.log_thread()
        if th is None:
            th = await get_or_create_log_thread()
        if th is None:
            log.warning("Log thread not available")
            return
        try:
            await th.send(text, allowed_mentions=NO_PINGS)
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
        await ensure_single_panel()
        await get_or_create_log_thread()
        log.info("Panel ensured in channel %s; log thread ensured.", PANEL_CHANNEL_ID)

    @bot.event
    async def on_message(message: discord.Message):
        # Houd paneel onderaan in het paneelkanaal
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
                if user.id != ST.current_user_id and user.id not in ST.queue:
                    ST.queue.append(user.id)
            await edit_panel_from_inter(inter)

    async def handle_found(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if ST.current_user_id and ST.current_user_id == user.id:
                ts = int(discord.utils.utcnow().timestamp())
                await append_log_line(f"• {user.mention} ✅ <t:{ts}:t>")
                await stop_only()
            await edit_panel_from_inter(inter)

    async def handle_next(inter: Interaction):
        user = inter.user
        async with ST.lock:
            if user.id != ST.current_user_id and user.id not in ST.queue:
                ST.queue.append(user.id)
            await edit_panel_from_inter(inter)

    async def handle_reset(inter: Interaction):
        actor = inter.user
        async with ST.lock:
            if ST.current_user_id:
                target_id = ST.current_user_id
                await stop_only()  # geen auto-start
                ts = int(discord.utils.utcnow().timestamp())
                await append_log_line(f"• <@{target_id}> ❌ by {actor.mention} <t:{ts}:t>")
            await edit_panel_from_inter(inter)

    # ==================== commands ====================
    @bot.command(name="panel")
    async def panel_cmd(ctx: commands.Context):
        """Maak paneel opnieuw (state blijft behouden). Mag in elk kanaal."""
        async with ST.lock:
            await ensure_single_panel()
            await get_or_create_log_thread()
        if isinstance(ctx.channel, discord.TextChannel) and ctx.channel.id == PANEL_CHANNEL_ID:
            try:
                await ctx.message.delete()
            except Exception:
                pass

    @bot.command(name="clear")
    async def clear_cmd(ctx: commands.Context, member: discord.Member):
        """
        Haal precies deze gebruiker uit het paneel:
        - als hij/zij aan het zoeken is -> stoppen
        - als hij/zij in de queue staat -> verwijderen
        Logt wie dit deed, met tijdstempel.
        """
        actor = ctx.author
        target_id = member.id
        did_anything = False

        async with ST.lock:
            # als current -> stop
            if ST.current_user_id == target_id:
                await stop_only()
                did_anything = True
                ts = int(discord.utils.utcnow().timestamp())
                await append_log_line(f"• {member.mention} ❌ by {actor.mention} <t:{ts}:t>")

            # als in queue -> remove
            if target_id in ST.queue:
                ST.queue = [uid for uid in ST.queue if uid != target_id]
                did_anything = True
                ts = int(discord.utils.utcnow().timestamp())
                await append_log_line(f"• {member.mention} 🗑️ removed by {actor.mention} <t:{ts}:t>")

            # panel bijwerken
            if did_anything:
                ch = ST.panel_channel()
                if ch:
                    # probeer in-place; valt zo nodig terug op fresh panel
                    await render_panel_in_place()
            else:
                # niets te doen: still update panel silently
                await render_panel_in_place()

        # ruim het commando op als het in het paneelkanaal stond
        if isinstance(ctx.channel, discord.TextChannel) and ctx.channel.id == PANEL_CHANNEL_ID:
            try:
                await ctx.message.delete()
            except Exception:
                pass

    @bot.command(name="clearpanel")
    async def clearpanel_cmd(ctx: commands.Context):
        """
        (Bestaat nog steeds, maar leeg alleen het HELE paneel.)
        Gebruik normaal liever: !clear @user
        """
        ch = ST.panel_channel()
        if ch is None:
            return
        async with ST.lock:
            ST.clear()
            await ensure_single_panel()
            await get_or_create_log_thread()

        if isinstance(ctx.channel, discord.TextChannel) and ctx.channel.id == PANEL_CHANNEL_ID:
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