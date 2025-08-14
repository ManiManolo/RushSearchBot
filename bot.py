import os
import asyncio
import logging
import threading
from typing import Dict, List, Optional

import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction, TextStyle
from flask import Flask

# ================== Logging ==================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rushsearchbot")

# ================== Config ===================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN ontbreekt in environment variables.")

# Zoek-timeout (in seconden) waarna we automatisch resetten en de volgende starten
SEARCH_TIMEOUT_SEC = int(os.getenv("SEARCH_TIMEOUT_SEC", "600"))  # default 10 min
# Countdown (in seconden) na 'found', 'reset' of timeout
HANDOVER_DELAY_SEC = int(os.getenv("HANDOVER_DELAY_SEC", "10"))  # default 10 sec
# Self-ping aan/uit
ENABLE_SELF_PING = os.getenv("SELF_PING", "true").lower() not in {"0", "false", "no"}

# ================== Discord Setup =============
intents = discord.Intents.default()
intents.message_content = True  # Zet ook aan in Developer Portal (Privileged Intents)
bot = commands.Bot(command_prefix="!", intents=intents)

# ================== State per kanaal ==========
class PanelState:
    """Beheert status voor één kanaal/paneel."""
    def __init__(self, channel_id: int):
        self.channel_id: int = channel_id
        self.panel_message_id: Optional[int] = None
        self.current_user_id: Optional[int] = None   # Wie is nu 'Searching'
        self.queue: List[int] = []                   # Volgorde van Next
        self.lock = asyncio.Lock()                   # Voorkomt dubbele 'Search'
        self.search_task: Optional[asyncio.Task] = None  # Timeout-task
        self.handover_task: Optional[asyncio.Task] = None # 10s handover
        self.last_messages: List[int] = []           # optioneel: opruimen

    def in_queue(self, user_id: int) -> bool:
        return user_id in self.queue

    def is_current(self, user_id: int) -> bool:
        return self.current_user_id == user_id

    def channel(self) -> Optional[discord.TextChannel]:
        return bot.get_channel(self.channel_id)

# Kanaal-id -> PanelState
PANELS: Dict[int, PanelState] = {}

# ================== Helper functies ===========
async def send_panel(state: PanelState, *, fresh: bool = True) -> discord.Message:
    """Plaats of ververs het knoppenpaneel in dit kanaal.
       fresh=True: stuur nieuw bericht en verwijder het oude (wens van gebruiker)."""
    channel = state.channel()
    if not channel:
        raise RuntimeError(f"Channel {state.channel_id} niet gevonden.")

    view = SearchView(state.channel_id)
    content = build_status_text(state)
    msg: discord.Message

    if fresh:
        # Verwijder het vorige paneel-bericht (als het bestaat)
        if state.panel_message_id:
            try:
                old = await channel.fetch_message(state.panel_message_id)
                await old.delete()
            except Exception:
                pass
        msg = await channel.send(content, view=view)
        state.panel_message_id = msg.id
    else:
        # Edit bestaand paneel
        if state.panel_message_id:
            try:
                msg = await channel.fetch_message(state.panel_message_id)
                await msg.edit(content=content, view=view)
                return msg
            except Exception:
                pass
        msg = await channel.send(content, view=view)
        state.panel_message_id = msg.id

    return msg

def build_status_text(state: PanelState) -> str:
    """Maak status-tekst voor boven het paneel."""
    parts = []
    if state.current_user_id:
        parts.append(f"🔎 **Searching**: <@{state.current_user_id}>")
    else:
        parts.append("🟦 **Searching**: *niemand*")

    if state.queue:
        q = " → ".join(f"<@{uid}>" for uid in state.queue[:10])
        more = f" (+{len(state.queue)-10} meer)" if len(state.queue) > 10 else ""
        parts.append(f"🟡 **Queue**: {q}{more}")
    else:
        parts.append("🟡 **Queue**: *leeg*")

    parts.append("\n**Knoppen:** 🔵 *Search* — ✅ *Found* — 🟡 *Next*")
    parts.append("*(Alleen de huidige zoeker mag **Found** drukken. Anderen gebruiken **Next** om in de wachtrij te komen.)*")
    return "\n".join(parts)

async def start_search_for(state: PanelState, user_id: int):
    """Maak deze gebruiker 'current' en start/refresh de timeout-task."""
    state.current_user_id = user_id

    # Cancel bestaande timeout/handover
    if state.search_task and not state.search_task.done():
        state.search_task.cancel()
    if state.handover_task and not state.handover_task.done():
        state.handover_task.cancel()

    # Start nieuwe timeout-task
    state.search_task = asyncio.create_task(search_timeout_runner(state))

    # Ververs paneel (nieuw bericht, per wens)
    await send_panel(state, fresh=True)

async def search_timeout_runner(state: PanelState):
    """Wacht SEARCH_TIMEOUT_SEC; als nog steeds dezelfde zoeker actief is -> handover."""
    try:
        await asyncio.sleep(SEARCH_TIMEOUT_SEC)
    except asyncio.CancelledError:
        return

    # Alleen doorzetten als er nog steeds iemand 'current' is
    if state.current_user_id:
        ch = state.channel()
        if ch:
            await ch.send(f"⏰ **Timeout** voor <@{state.current_user_id}>. Wissel over **in {HANDOVER_DELAY_SEC}s**...")
        await schedule_handover(state)

async def schedule_handover(state: PanelState):
    """Start de 10s countdown en zet daarna de volgende in de rij automatisch live."""
    if state.handover_task and not state.handover_task.done():
        state.handover_task.cancel()
    state.handover_task = asyncio.create_task(handover_runner(state))

async def handover_runner(state: PanelState):
    # Countdown
    try:
        for remaining in range(HANDOVER_DELAY_SEC, 0, -1):
            ch = state.channel()
            if ch:
                await ch.send(f"⏳ Wissel in **{remaining}**s...")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return

    # Voer de wissel uit
    await perform_handover(state)

async def perform_handover(state: PanelState):
    """Zet current naar None en neem volgende uit de queue (indien aanwezig)."""
    prev = state.current_user_id
    state.current_user_id = None

    user_to_start = None
    if state.queue:
        user_to_start = state.queue.pop(0)

    if user_to_start:
        await start_search_for(state, user_to_start)
        ch = state.channel()
        if ch:
            await ch.send(f"➡️ **Aan de beurt:** <@{user_to_start}>. Succes met zoeken!")
    else:
        # Niemand in de wachtrij
        if state.search_task and not state.search_task.done():
            state.search_task.cancel()
        await send_panel(state, fresh=True)
        ch = state.channel()
        if ch:
            await ch.send("🟰 Wachtrij is leeg. Je kunt **Search** drukken om te starten.")

def get_or_create_state(channel_id: int) -> PanelState:
    if channel_id not in PANELS:
        PANELS[channel_id] = PanelState(channel_id)
    return PANELS[channel_id]

# ================== UI: Knoppen =================
class SearchView(ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)  # persistent view
        self.channel_id = channel_id

        # 🔵 Search (primary)
        self.add_item(ui.Button(label="🔎 Search", style=ButtonStyle.primary, custom_id="rsb_search"))
        # ✅ Found (success)
        self.add_item(ui.Button(label="🎮 Found", style=ButtonStyle.success, custom_id="rsb_found"))
        # 🟡 Next (secondary; Discord kent geen gele stijl)
        self.add_item(ui.Button(label="🟡 Next", style=ButtonStyle.secondary, custom_id="rsb_next"))
        # 🔁 Reset (danger) — handig als iets vastloopt
        self.add_item(ui.Button(label="🔁 Reset", style=ButtonStyle.danger, custom_id="rsb_reset"))

    async def interaction_check(self, interaction: Interaction) -> bool:
        # Alleen toelaten in het kanaal waarvoor dit paneel bedoeld is
        return interaction.channel and interaction.channel.id == self.channel_id

    @ui.button(label="hidden", style=ButtonStyle.secondary)  # placeholder; nooit zichtbaar
    async def _hidden(self, *_):
        pass

    @discord.ui.button  # deze decorator wordt niet gebruikt; we gebruiken custom_ids boven
    async def dummy(self, *_):
        pass

    @discord.ui.button
    async def _dummy2(self, *_):
        pass

    async def on_error(self, error: Exception, item: ui.Item, interaction: Interaction) -> None:
        log.exception("Fout in UI: %r", error)
        try:
            await interaction.response.send_message("Er ging iets mis met de knoppen. Probeer het nog eens.", ephemeral=True)
        except Exception:
            pass

    # We vangen de klikken via on_interaction hieronder in de bot events (zodat custom_ids werken)


# ================== Event handlers ==============
@bot.event
async def on_ready():
    log.info("✅ Ingelogd als %s (id=%s, latency=%.3fs)", bot.user, bot.user.id, bot.latency)

    # Persistent view registreren (zodat knoppen blijven werken na restart)
    # We registreren 1 generic view; per klik wordt state via channel-id opgehaald.
    # (custom_ids zijn vast, dus dit werkt ook na reboot)
    try:
        # We registreren een 'dummy' view voor elk actief kanaal bij eerste gebruik;
        # hier is geen vaste lijst, dus alleen globale registratie:
        bot.add_view(SearchView(channel_id=0))  # channel_id wordt niet gebruikt bij global registry
    except Exception:
        pass

# Globale on_interaction om onze 4 custom_ids af te handelen
@bot.event
async def on_interaction(interaction: Interaction):
    if not interaction.type == discord.InteractionType.component:
        return
    cid = interaction.data.get("custom_id")
    if cid not in {"rsb_search", "rsb_found", "rsb_next", "rsb_reset"}:
        return

    channel = interaction.channel
    if channel is None or not isinstance(channel, discord.TextChannel):
        return

    state = get_or_create_state(channel.id)

    # Controleer dat de klik bij dit kanaal hoort (panel kan per kanaal verschillen)
    # We laten het toe; het paneel bericht zelf is al kanaalgebonden.

    if cid == "rsb_search":
        await handle_search(interaction, state)
    elif cid == "rsb_found":
        await handle_found(interaction, state)
    elif cid == "rsb_next":
        await handle_next(interaction, state)
    elif cid == "rsb_reset":
        await handle_reset(interaction, state)

async def handle_search(inter: Interaction, state: PanelState):
    user = inter.user
    # Probeer lock te pakken (voorkomt race)
    async with state.lock:
        if state.current_user_id:
            # Er is al iemand aan de beurt
            if state.current_user_id == user.id:
                # Jij bent het al
                try:
                    await inter.response.send_message("Je bent al aan het zoeken ✅", ephemeral=True)
                except Exception:
                    pass
                return
            else:
                # Zet gebruiker eventueel in de wachtrij
                if user.id not in state.queue:
                    state.queue.append(user.id)
                try:
                    await inter.response.send_message("Iemand is al aan het zoeken. Je staat in de wachtrij 🟡", ephemeral=True)
                except Exception:
                    pass
                # Paneel verversen (nieuw bericht)
                await send_panel(state, fresh=True)
                return

        # Niemand zoekt -> jij wordt current
        await start_search_for(state, user.id)
        # Paneel is al ververst in start_search_for
        # Maak een 'nieuwe' paneelboodschap per wens
        try:
            await inter.response.send_message(f"🔎 **{user.mention}** is begonnen met zoeken!", ephemeral=True)
        except Exception:
            pass
        ch = state.channel()
        if ch:
            await ch.send(f"🔵 **Searching gestart door {user.mention}**")

async def handle_found(inter: Interaction, state: PanelState):
    user = inter.user
    if not state.current_user_id:
        try:
            await inter.response.send_message("Er is nu niemand aan het zoeken.", ephemeral=True)
        except Exception:
            pass
        return

    if state.current_user_id != user.id:
        try:
            await inter.response.send_message("Alleen de huidige zoeker kan **Found** drukken.", ephemeral=True)
        except Exception:
            pass
        return

    # Geldige found — stop huidige en schedule handover
    try:
        await inter.response.send_message("🎉 Nice! We wisselen zo door.", ephemeral=True)
    except Exception:
        pass
    ch = state.channel()
    if ch:
        await ch.send(f"✅ **Found door {user.mention}** — Wissel **in {HANDOVER_DELAY_SEC}s**...")

    await schedule_handover(state)
    # Per wens: direct nieuw paneelbericht
    await send_panel(state, fresh=True)

async def handle_next(inter: Interaction, state: PanelState):
    user = inter.user

    # Als niemand zoekt en er is geen queue -> je kan meteen starten na korte handover
    if not state.current_user_id and not state.queue:
        # Start direct (zonder handover), maar we houden UX hetzelfde: 10s countdown
        if user.id not in state.queue:
            state.queue.append(user.id)
        try:
            await inter.response.send_message(f"Je staat **vooraan**. We starten **in {HANDOVER_DELAY_SEC}s**...", ephemeral=True)
        except Exception:
            pass
        await schedule_handover(state)
        await send_panel(state, fresh=True)
        return

    # Voeg toe aan wachtrij als nog niet erin
    if user.id in state.queue:
        try:
            await inter.response.send_message("Je staat al in de wachtrij 🟡", ephemeral=True)
        except Exception:
            pass
        return

    state.queue.append(user.id)
    try:
        await inter.response.send_message("Toegevoegd aan de wachtrij 🟡", ephemeral=True)
    except Exception:
        pass
    await send_panel(state, fresh=True)

async def handle_reset(inter: Interaction, state: PanelState):
    # Reset huidige + handover plannen zodat de volgende automatisch start
    try:
        await inter.response.send_message("Reset ontvangen. Wissel zo door.", ephemeral=True)
    except Exception:
        pass
    ch = state.channel()
    if ch:
        await ch.send(f"🔁 Reset door {inter.user.mention} — wissel **in {HANDOVER_DELAY_SEC}s**...")

    await schedule_handover(state)
    await send_panel(state, fresh=True)

# ================== Commands ====================
@bot.command()
async def panel(ctx: commands.Context):
    """(Re)plaats het knoppenpaneel in dit kanaal."""
    state = get_or_create_state(ctx.channel.id)
    await send_panel(state, fresh=True)
    await ctx.send("🎛️ Paneel geplaatst/ververst.", delete_after=5)

@bot.command()
async def status(ctx: commands.Context):
    """Toon de huidige status/queue in dit kanaal."""
    state = get_or_create_state(ctx.channel.id)
    await ctx.send(build_status_text(state))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def resetqueue(ctx: commands.Context):
    """Leeg de wachtrij en stop de huidige zoeker (admin)."""
    state = get_or_create_state(ctx.channel.id)
    state.queue.clear()
    state.current_user_id = None
    if state.search_task and not state.search_task.done():
        state.search_task.cancel()
    if state.handover_task and not state.handover_task.done():
        state.handover_task.cancel()
    await send_panel(state, fresh=True)
    await ctx.send("🧹 Wachtrij geleegd en status gereset.")

# ================== Webserver (Render) ==========
app = Flask(__name__)

@app.get("/")
def health():
    return "ok", 200

def run_webserver():
    port = int(os.environ.get("PORT", "8080"))
    log.info("🌐 Start webserver op 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)

async def self_ping():
    """Ping elke 5 min de lokale health endpoint, houdt de dyno levend zonder Cloudflare-ruis."""
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://127.0.0.1:{port}/"
    import requests
    while True:
        try:
            requests.get(url, timeout=5)
            log.debug("🔄 Self-ping: %s", url)
        except Exception as e:
            log.debug("Self-ping error: %r", e)
        await asyncio.sleep(300)

# ================== Main ========================
async def main():
    t = threading.Thread(target=run_webserver, daemon=True)
    t.start()
    if ENABLE_SELF_PING:
        asyncio.create_task(self_ping())
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Stop aangevraagd, sluit af...")