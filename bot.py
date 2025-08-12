# bot.py
import os
import asyncio
import discord
from discord.ext import commands
from discord.ui import View, Button
from flask import Flask
import threading
import requests

TOKEN = os.getenv("DISCORD_TOKEN")  # Zorg dat deze in Render staat ingesteld
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))  # Zet je kanaal-ID hier of in Render env

# ---- Discord Bot ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class SearchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="Searching", style=discord.ButtonStyle.green))
        self.add_item(Button(label="Found", style=discord.ButtonStyle.blurple))

async def send_new_buttons(channel):
    """Verwijdert oude knoppen en stuurt nieuwe"""
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and msg.components:
            await msg.delete()
    await channel.send(" ", view=SearchView())  # Los bericht, geen reply

@bot.event
async def on_ready():
    print(f"✅ Ingelogd als {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await send_new_buttons(channel)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    channel = bot.get_channel(CHANNEL_ID)
    if message.channel.id == CHANNEL_ID:
        await send_new_buttons(channel)

# ---- Mini Webserver ----
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_webserver():
    app.run(host='0.0.0.0', port=8080)

# ---- Zelf Pingen ----
async def self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL")  # Render zet dit automatisch
    if not url:
        print("⚠️ Geen RENDER_EXTERNAL_URL gevonden, self-ping werkt niet.")
        return
    while True:
        try:
            requests.get(url)
            print(f"🔄 Self-ping naar {url}")
        except Exception as e:
            print(f"Ping fout: {e}")
        await asyncio.sleep(300)  # elke 5 minuten

async def main():
    threading.Thread(target=run_webserver).start()
    asyncio.create_task(self_ping())
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())