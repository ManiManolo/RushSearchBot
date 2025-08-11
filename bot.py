import os
import discord
from discord.ext import commands
import asyncio
from aiohttp import web

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_NAME = "🔎searching"

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Hier komt je SearchButtons class en event handlers, simpel voorbeeld:
class SearchButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.blurple)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} 🔎Search🔍")

    @discord.ui.button(label="Found", style=discord.ButtonStyle.green)
    async def found(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} ✅Found✅")

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} ⏭️Next⏮️")


@bot.event
async def on_ready():
    print(f"Bot ingelogd als {bot.user}")

@bot.tree.command(name="place_buttons", description="Plaats de knoppen.")
async def place_buttons(interaction: discord.Interaction):
    if interaction.channel.name != CHANNEL_NAME:
        await interaction.response.send_message(f"Gebruik dit in het kanaal **{CHANNEL_NAME}**.", ephemeral=True)
        return
    await interaction.channel.send(view=SearchButtons())
    await interaction.response.send_message("Knoppen geplaatst!", ephemeral=True)

# Eenvoudige aiohttp webserver
async def handle(request):
    return web.Response(text="Bot is running!")

async def start_webserver():
    app = web.Application()
    app.add_routes([web.get('/', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Webserver draait op http://0.0.0.0:{port}")

async def main():
    await start_webserver()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())