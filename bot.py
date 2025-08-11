import os
import discord
from discord.ext import commands
from discord import app_commands
from aiohttp import web
import asyncio

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_NAME = "🔎searching"

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

class SearchButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.last_message = None

    async def refresh_buttons(self, interaction: discord.Interaction):
        channel = interaction.channel

        # Oude knoppen verwijderen
        if self.last_message:
            try:
                await self.last_message.delete()
            except discord.NotFound:
                pass

        # Nieuwe knoppen plaatsen onderaan
        self.last_message = await channel.send(view=SearchButtons())

    @discord.ui.button(label="Search", style=discord.ButtonStyle.blurple)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔎 Search gestart")
        await self.refresh_buttons(interaction)

    @discord.ui.button(label="Found", style=discord.ButtonStyle.green)
    async def found(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Found gemeld")
        await self.refresh_buttons(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏭️ Volgende gezocht")
        await self.refresh_buttons(interaction)

@bot.event
async def on_ready():
    print(f"Bot ingelogd als {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == CHANNEL_NAME:
        # Oude knoppen verwijderen
        async for msg in message.channel.history(limit=50):
            if msg.components:
                try:
                    await msg.delete()
                except discord.Forbidden:
                    pass

        # Nieuwe knoppen plaatsen
        await message.channel.send(view=SearchButtons())

    await bot.process_commands(message)

# Simpele webserver om de bot wakker te houden
async def handle(request):
    return web.Response(text="Bot is alive!")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

async def main():
    # Start webserver en bot tegelijk
    await start_webserver()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())