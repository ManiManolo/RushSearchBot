# bot.py
import os
import asyncio
import discord
from discord.ext import commands
from flask import Flask
import threading
import requests

TOKEN = os.getenv("DISCORD_TOKEN")  # Zet deze in Render environment variables
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))  # Zet je kanaal-ID in Render env

# ---- Discord Bot ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


class SearchButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def replace_buttons(self, interaction: discord.Interaction, text: str):
        """Verwijder oude knoppen, stuur status en nieuwe knoppen"""
        await interaction.message.delete()
        await interaction.channel.send(text)
        await send_new_buttons(interaction.channel)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.green)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.replace_buttons(interaction, f"{interaction.user.mention} 🕵️‍♂️ searching 🕵️‍♂️")

    @discord.ui.button(label="Found", style=discord.ButtonStyle.blurple)
    async def found_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.replace_buttons(interaction, f"{interaction.user.mention} ✅ found ✅")

    @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.replace_buttons(interaction, f"{interaction.user.mention} ⏭️ next ⏮️")


async def send_new_buttons(channel):
    """Verwijder oude knoppen en stuur nieuwe"""
    async for msg in channel.history(limit=50):
        if msg.author == bot.user and msg.components:
            await msg.delete()
    await channel.send(" ", view=SearchButtons())  # Nieuw bericht, geen reply


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
        # Controleer of laatste bericht geen knoppen heeft
        last_msg = await message.channel.history(limit=1).flatten()
        if not last_msg[0].components:
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
    url = os.getenv("RENDER_EXTERNAL_URL")  # Render zet dit automatisch
    if not url:
        print("⚠️ No RENDER_EXTERNAL_URL found, self-ping will not work.")
        return
    while True:
        try:
            requests.get(url)
            print(f"🔄 Self-ping to {url}")
        except Exception as e:
            print(f"Ping error: {e}")
        await asyncio.sleep(300)  # elke 5 minuten


async def main():
    threading.Thread(target=run_webserver).start()
    asyncio.create_task(self_ping())
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())