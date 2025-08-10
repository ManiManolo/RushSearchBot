import discord
from discord.ext import commands
from discord.ui import View, Button
import os
from flask import Flask
import threading

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Webserver voor Render/UptimeRobot
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_webserver():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run_webserver)
    t.start()

# Knoppen weergave
class SearchView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(Button(label="Search", style=discord.ButtonStyle.primary, custom_id="search"))
        self.add_item(Button(label="Found", style=discord.ButtonStyle.success, custom_id="found"))
        self.add_item(Button(label="Next", style=discord.ButtonStyle.secondary, custom_id="next"))

@bot.event
async def on_ready():
    print(f"Bot is ingelogd als {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await message.channel.purge(limit=1)
    await message.channel.send("Knoppen:", view=SearchView())

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.data["custom_id"] == "search":
        await interaction.response.send_message(f"{interaction.user.mention} is searching...", delete_after=5)
    elif interaction.data["custom_id"] == "found":
        await interaction.response.send_message(f"{interaction.user.mention} has found it!", delete_after=5)
    elif interaction.data["custom_id"] == "next":
        await interaction.response.send_message(f"{interaction.user.mention} is next!", delete_after=5)

    await interaction.message.edit(view=SearchView())

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
