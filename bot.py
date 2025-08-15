import os
import discord
from discord.ext import commands
from discord.ui import View, Button
from datetime import datetime
import pytz

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

current_searcher = None
queue = []
logbook = []  # Lijst met tuples: (naam, tijd)

def format_panel():
    searching_line = f"**Searching:** {current_searcher[0]} ({current_searcher[1].strftime('%H:%M')})" if current_searcher else "**Searching:** -"
    queue_line = "\n".join(queue) if queue else "-"
    log_lines = "\n".join([f"{name} ({time.strftime('%H:%M')})" for name, time in logbook[-20:]]) if logbook else "-"
    return f"{searching_line}\n\n**Queue:**\n{queue_line}\n\n**Logbook:**\n||{log_lines}||"

class PanelView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="🔎 Search", style=discord.ButtonStyle.primary, custom_id="search"))
        self.add_item(Button(label="🎮 Found", style=discord.ButtonStyle.success, custom_id="found"))
        self.add_item(Button(label="🔁 Reset", style=discord.ButtonStyle.danger, custom_id="reset"))
        self.add_item(Button(label="⏭ Next", style=discord.ButtonStyle.secondary, custom_id="next"))

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await ensure_panel(channel)

async def ensure_panel(channel):
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            await msg.edit(content=format_panel(), view=PanelView())
            return
    await channel.send(content=format_panel(), view=PanelView())

@bot.event
async def on_interaction(interaction: discord.Interaction):
    global current_searcher, queue, logbook

    if interaction.type != discord.InteractionType.component:
        return

    user_name = interaction.user.display_name

    if interaction.data["custom_id"] == "search":
        if current_searcher is None:
            current_searcher = (user_name, datetime.now())
        elif user_name not in queue:
            queue.append(user_name)

    elif interaction.data["custom_id"] == "found":
        if current_searcher and current_searcher[0] == user_name:
            logbook.append((user_name, datetime.now()))
            current_searcher = None

    elif interaction.data["custom_id"] == "reset":
        if current_searcher:
            current_searcher = None

    elif interaction.data["custom_id"] == "next":
        if not current_searcher and queue:
            current_searcher = (queue.pop(0), datetime.now())

    await interaction.response.edit_message(content=format_panel(), view=PanelView())

bot.run(TOKEN)