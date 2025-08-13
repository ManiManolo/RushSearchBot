# bot.py
import os
import asyncio
import discord
import time
from discord.ext import commands
from flask import Flask
import threading
import requests

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

class SearchButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def remove_user_messages(self, channel, user, keyword):
        """Verwijder alle berichten van een user met een bepaald keyword."""
        async for msg in channel.history(limit=50):
            if msg.author == bot.user and user.mention in msg.content and keyword in msg.content:
                await msg.delete()

    @discord.ui.button(label="Search", style=discord.ButtonStyle.green)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Verwijder vorige 'next' berichten van deze user
        await self.remove_user_messages(interaction.channel, interaction.user, "⏭️next⏮️")
        ts = int(time.time())
        await interaction.channel.send(f"<t:{ts}:t> {interaction.user.mention} 🕵️‍♂️ searching 🕵️‍♂️")
        await send_new_buttons(interaction.channel)

    @discord.ui.button(label="Found", style=discord.ButtonStyle.blurple)
    async def found_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Verwijder vorige 'search' berichten van deze user
        await self.remove_user_messages(interaction.channel, interaction.user, "🕵️‍♂️ searching 🕵️‍♂️")
        ts = int(time.time())
        await interaction.channel.send(f"<t:{ts}:t> {interaction.user.mention} ✅found✅")
        await send_new_buttons(interaction.channel)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ts = int(time.time())
        await interaction.channel.send(f"<t:{ts}:t> {interaction.user.mention} ⏭️next⏮️")
        await send_new_buttons(interaction.channel)

async def send_new_buttons(channel):
    """Verwijder oude knoppen en stuur nieuwe"""
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and msg.components:
            await msg.delete()
    await channel.send(" ", view=SearchButtons())

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await send_new_buttons(channel)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.channel.id == CHANNEL_ID:
        await send_new_buttons(message.channel)

# ---- Mini Webserver ----
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_webserver():
    app.run(host='0.0.0.0', port=8080)

# ---- Zelf Pingen ----
async def self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        print("⚠️ No RENDER_EXTERNAL_URL found, self-ping will not work.")
        return
    while True:
        try:
            requests.get(url)
            print(f"🔄 Self-ping to {url}")
        except Exception as e:
            print(f"Ping error: {e}")
        await asyncio.sleep(300)

async def main():
    threading.Thread(target=run_webserver).start()
    asyncio.create_task(self_ping())
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())