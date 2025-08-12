# bot.py
import os
import threading
import asyncio
import requests
from flask import Flask
import discord
from discord.ext import commands

# config via env
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # vul kanaal-ID in Render env
PING_INTERVAL = int(os.getenv("SELF_PING_INTERVAL", "300"))  # sec

# discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# lock om race-conditions te voorkomen
buttons_lock = asyncio.Lock()

# --- View met 3 knoppen en callbacks ---
class SearchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Searching", style=discord.ButtonStyle.blurple, custom_id="rush_searching")
    async def searching(self, interaction: discord.Interaction, button: discord.ui.Button):
        # stuur een losstaand bericht (geen reply)
        try:
            await interaction.response.send_message("🔎 Search gestart")
        except Exception:
            # als response al afgehandeld is (safety), probeer followup
            try:
                await interaction.followup.send("🔎 Search gestart")
            except Exception:
                pass
        await send_new_buttons(interaction.channel)

    @discord.ui.button(label="Found", style=discord.ButtonStyle.green, custom_id="rush_found")
    async def found(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message("✅ Found gemeld")
        except Exception:
            try:
                await interaction.followup.send("✅ Found gemeld")
            except Exception:
                pass
        await send_new_buttons(interaction.channel)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="rush_next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message("⏭️ Volgende gezocht")
        except Exception:
            try:
                await interaction.followup.send("⏭️ Volgende gezocht")
            except Exception:
                pass
        await send_new_buttons(interaction.channel)

# --- Functie die oude knoppen verwijdert en nieuwe onderaan plaatst ---
async def send_new_buttons(channel: discord.TextChannel):
    async with buttons_lock:
        try:
            # verwijder oudere berichten met components (limit verhoogd voor zekerheid)
            async for msg in channel.history(limit=200):
                if msg.components:
                    try:
                        await msg.delete()
                    except discord.Forbidden:
                        # geen permissie om te verwijderen -> break of continue
                        print("Geen permissie om berichten te verwijderen in kanaal.")
                        break
                    except Exception:
                        pass

            # verstuur nieuwe knopset (geen content, alleen componenten)
            await channel.send(view=SearchView())
        except Exception as e:
            print("Fout in send_new_buttons:", e)

# --- Events ---
@bot.event
async def on_ready():
    print(f"✅ Ingelogd als {bot.user} (id: {bot.user.id})")
    # registreer persistent view zodat knoppen blijven werken na restart
    bot.add_view(SearchView())
    # plaats meteen knoppen in kanaal als CHANNEL_ID is gezet
    if CHANNEL_ID:
        ch = bot.get_channel(CHANNEL_ID)
        if ch:
            await send_new_buttons(ch)
        else:
            print("Kanaal niet gevonden bij on_ready. Controleer CHANNEL_ID.")

@bot.event
async def on_message(message: discord.Message):
    # negeer bot berichten
    if message.author.bot:
        return

    if message.channel.id == CHANNEL_ID:
        # plaats nieuwe knoppen bij elk nieuw bericht in het kanaal
        await send_new_buttons(message.channel)

    await bot.process_commands(message)

# --- Mini webserver (Flask) voor Render keep-alive ---
app = Flask("bot")

@app.route("/")
def home():
    return "Bot is alive!"

def run_webserver():
    # Flask dev server — enkel voor keep-alive. Render gebruikt dit als healthcheck.
    app.run(host="0.0.0.0", port=8080)

# --- self ping zodat Render de service niet slaapt ---
async def self_ping_loop():
    url = os.getenv("RENDER_EXTERNAL_URL")  # Render zet dit automatisch
    if not url:
        print("⚠️ RENDER_EXTERNAL_URL niet gevonden; self-ping is uit.")
        return
    while True:
        try:
            requests.get(url, timeout=10)
            print(f"🔄 Self-ping naar {url}")
        except Exception as e:
            print("Self-ping fout:", e)
        await asyncio.sleep(PING_INTERVAL)

# --- Entrypoint ---
async def main():
    if TOKEN is None:
        print("❌ DISCORD_TOKEN is niet gezet in environment variables.")
        return

    # start webserver in aparte thread
    threading.Thread(target=run_webserver, daemon=True).start()

    # start self-ping loop
    asyncio.create_task(self_ping_loop())

    # start bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())