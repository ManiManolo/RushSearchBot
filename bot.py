import os
import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optioneel

CHANNEL_NAME = "🔎searching"

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# Knoppen view
class SearchButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.blurple)
    async def search(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} 🔎Search🔍")
        await self.refresh_buttons(interaction)

    @discord.ui.button(label="Found", style=discord.ButtonStyle.green)
    async def found(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} ✅Found✅")
        await self.refresh_buttons(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{interaction.user.mention} ⏭️Next⏮️")
        await self.refresh_buttons(interaction)

    async def refresh_buttons(self, interaction: discord.Interaction):
        # Oude knoppen verwijderen
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        # Nieuwe knoppen plaatsen onderaan kanaal
        channel = interaction.guild.get_channel(interaction.channel.id)
        await channel.send(view=SearchButtons())


# Bij opstarten
@bot.event
async def on_ready():
    print(f"Bot ingelogd als {bot.user}")
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Slash commands gesynchroniseerd met guild {GUILD_ID}")
    else:
        await bot.tree.sync()
        print("Slash commands globaal gesynchroniseerd")


# Knoppen terugplaatsen als iemand in kanaal iets zegt
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.name == CHANNEL_NAME:
        await message.channel.purge(limit=1, check=lambda m: isinstance(m.components, list) and m.components)
        await message.channel.send(view=SearchButtons())
    await bot.process_commands(message)


# Slash command om knoppen handmatig te plaatsen
@bot.tree.command(name="place_buttons", description="Plaats de Search/Found/Next knoppen in het kanaal.")
async def place_buttons(interaction: discord.Interaction):
    if interaction.channel.name != CHANNEL_NAME:
        await interaction.response.send_message(f"Gebruik dit in het kanaal **{CHANNEL_NAME}**.", ephemeral=True)
        return
    await interaction.channel.send(view=SearchButtons())
    await interaction.response.send_message("Knoppen geplaatst!", ephemeral=True)


bot.run(TOKEN)