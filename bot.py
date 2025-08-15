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
            self.logbook: List[Tuple[int, int]] = []  # [(user_id, unix_ts)] max