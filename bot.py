import os
import discord
from discord.ext import commands
from datetime import datetime
from collections import deque

TOKEN = os.getenv("DISCORD_TOKEN")
LOG_THREAD_ID = int(os.getenv("LOG_THREAD_ID", "0"))  # jouw thread ID
CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

panel_message = None
searching_user = None
queue = []
log_entries = deque(maxlen=50)  # laatste 50 logs

# --- PANEL UPDATE ---
async def update_panel():
    global panel_message
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return
    
    searching_text = f"Searching: {searching_user.mention}" if searching_user else "Searching: -"
    queue_text = "\n".join([u.mention for u in queue]) if queue else "-"
    
    embed = discord.Embed(title="Search Panel", color=discord.Color.blue())
    embed.add_field(name="Searching", value=searching_text, inline=False)
    embed.add_field(name="\u200b", value="\u200b", inline=False)  # lege regel
    embed.add_field(name="Queue", value=queue_text, inline=False)
    
    if panel_message:
        try:
            await panel_message.edit(embed=embed)
        except discord.NotFound:
            panel_message = await channel.send(embed=embed)
    else:
        panel_message = await channel.send(embed=embed)

# --- LOG UPDATE ---
async def update_log():
    if LOG_THREAD_ID == 0:
        return
    try:
        thread = await bot.fetch_channel(LOG_THREAD_ID)
    except discord.NotFound:
        print("❌ Thread niet gevonden!")
        return
    
    log_text = "\n".join([f"{time} - {user}" for time, user in log_entries]) or "Nog geen logs."
    await thread.send(f"**Log:**\n{log_text}")

# --- COMMANDS ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await update_panel()
    await update_log()

@bot.command()
async def search(ctx):
    global searching_user
    if not searching_user:
        searching_user = ctx.author
        await update_panel()
    elif ctx.author not in queue:
        queue.append(ctx.author)
        await update_panel()

@bot.command()
async def found(ctx):
    global searching_user
    if ctx.author == searching_user:
        log_entries.append((datetime.now().strftime("%H:%M"), ctx.author.name))
        searching_user = None
        await update_panel()
        await update_log()

@bot.command()
async def reset(ctx):
    global searching_user
    if searching_user:
        searching_user = None
        await update_panel()

bot.run(TOKEN)