import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
import os

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
if GUILD_ID:
    try:
        GUILD_ID = int(GUILD_ID)
    except ValueError:
        GUILD_ID = 0
else:
    GUILD_ID = 0

SEARCHING_CHANNEL_NAME = "🔎searching"

class SearchView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.primary, custom_id="btn_search")
    async def search_button(self, interaction: discord.Interaction, button: Button):
        await handle_interaction(interaction, "🔎Search🔍")

    @discord.ui.button(label="Found", style=discord.ButtonStyle.success, custom_id="btn_found")
    async def found_button(self, interaction: discord.Interaction, button: Button):
        await handle_interaction(interaction, "✅Found✅")

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="btn_next")
    async def next_button(self, interaction: discord.Interaction, button: Button):
        await handle_interaction(interaction, "⏭️next⏮️")

async def handle_interaction(interaction: discord.Interaction, label: str):
    await interaction.response.send_message(f"{label} {interaction.user.mention}", ephemeral=False)
    bot = interaction.client
    channel = interaction.channel

    if bot.last_buttons_message:
        try:
            await bot.last_buttons_message.edit(view=None)
        except:
            pass

    view = SearchView()
    new_msg = await channel.send("Kies een optie:", view=view)
    bot.last_buttons_message = new_msg

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.last_buttons_message = None

    async def setup_hook(self):
        if GUILD_ID != 0:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Bot is ready als {bot.user}")

@bot.tree.command(name="sendbuttons", description="Stuur de knoppen in het kanaal 🔎searching")
async def send_buttons(interaction: discord.Interaction):
    channel = None
    for c in interaction.guild.text_channels:
        if c.name == SEARCHING_CHANNEL_NAME:
            channel = c
            break
    if not channel:
        await interaction.response.send_message(f"Kan kanaal '{SEARCHING_CHANNEL_NAME}' niet vinden.", ephemeral=True)
        return

    if bot.last_buttons_message:
        try:
            await bot.last_buttons_message.edit(view=None)
        except:
            pass

    view = SearchView()
    msg = await channel.send("Kies een optie:", view=view)
    bot.last_buttons_message = msg
    await interaction.response.send_message(f"Knoppenbericht gestuurd in {channel.mention}", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.name != SEARCHING_CHANNEL_NAME:
        return

    if bot.last_buttons_message:
        try:
            await bot.last_buttons_message.edit(view=None)
        except:
            pass

        view = SearchView()
        new_msg = await message.channel.send("Kies een optie:", view=view)
        bot.last_buttons_message = new_msg

    await bot.process_commands(message)

if not TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is not set.")
bot.run(TOKEN)
