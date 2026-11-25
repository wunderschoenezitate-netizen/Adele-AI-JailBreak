"""Discord sales front for AdeleAI Private.

Buyers press a button, pick a plan, and pay in LTC to a fresh address derived
from a public key (xpub/zpub). The server never holds a seed. When the payment
arrives, a single-use key is sent to the buyer by DM. The key is redeemed in the
Telegram bot, which grants the subscription.
"""
import asyncio
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
from decimal import ROUND_UP, Decimal
from urllib.request import Request, urlopen

import discord
from discord import ui

import addresses

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
SALES_CHANNEL_ID = int(os.environ["DISCORD_SALES_CHANNEL_ID"])
LTC_XPUB = os.environ["LTC_XPUB"]
DB = os.environ.get("DATABASE_PATH", "adeleai.sqlite3")
PLANS = {"30": ("5", 30), "90": ("13", 90), "365": ("48", 365)}
ORDER_TTL = 3600
CHECK_EVERY = 60
KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TG_BOT = "@AdlesesAI_bot"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_sales")


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS ltc_orders (
            order_id TEXT PRIMARY KEY, discord_user INTEGER NOT NULL, days INTEGER NOT NULL,
            eur TEXT NOT NULL, ltc_amount TEXT NOT NULL, idx INTEGER NOT NULL UNIQUE,
            address TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created INTEGER NOT NULL,
            expires INTEGER NOT NULL, key TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY, days INTEGER NOT NULL, order_id TEXT,
            used_by INTEGER, used_at INTEGER)""")
        conn.execute("CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL)")


def ltc_eur_rate() -> Decimal:
    req = Request("https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=eur",
                  headers={"User-Agent": "AdeleAIBot/1.0"})
    with urlopen(req, timeout=15) as response:
        return Decimal(str(json.load(response)["litecoin"]["eur"]))


def create_order(discord_user: int, plan: str) -> dict:
    eur, days = PLANS[plan]
    rate = ltc_eur_rate()
    amount = (Decimal(eur) / rate).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
    order_id = "LTC-" + secrets.token_hex(4).upper()
    now = int(time.time())
    with db() as conn:
        idx = conn.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM ltc_orders").fetchone()[0]
        address = addresses.address_from_xpub("LTC", LTC_XPUB, idx)
        conn.execute(
            "INSERT INTO ltc_orders (order_id, discord_user, days, eur, ltc_amount, idx, address, status, created, expires)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
            (order_id, discord_user, days, eur, str(amount), idx, address, now, now + ORDER_TTL))
    return {"order_id": order_id, "address": address, "amount": amount, "eur": eur, "days": days}


def received_ltc(address: str) -> Decimal:
    req = Request(f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}?confirmations=1",
                  headers={"User-Agent": "AdeleAIBot/1.0"})
    with urlopen(req, timeout=20) as response:
        return Decimal(json.load(response)["balance"]) / Decimal(10 ** 8)


def new_key() -> str:
    groups = ["".join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "ADL-" + "-".join(groups)


INSTRUCTIONS = (
    "✅ **Payment received. Here is your access key:**\n\n"
    "`{key}`\n\n"
    "**How to activate it:**\n"
    "1️⃣ Open {bot} on Telegram: https://t.me/AdlesesAI_bot\n"
    "2️⃣ Send `/start`\n"
    "3️⃣ Paste the key above as a normal message\n"
    "4️⃣ You get a one-time invite to the private channel\n\n"
    "⚠️ The key works only once. Keep it private.\n"
    "🆘 Problems? Tap **Support** in the Telegram bot."
)


async def settle_orders(client: discord.Client):
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT order_id, discord_user, days, ltc_amount, address, expires FROM ltc_orders WHERE status='PENDING'"
                ).fetchall()
            for order_id, user_id, days, amount, address, expires in rows:
                now = int(time.time())
                paid = False
                try:
                    paid = await asyncio.to_thread(received_ltc, address) >= Decimal(amount)
                except Exception:
                    log.exception("balance check failed for %s", order_id)
                if paid:
                    key = new_key()
                    with db() as conn:
                        conn.execute("INSERT INTO keys (key, days, order_id) VALUES (?, ?, ?)", (key, days, order_id))
                        conn.execute("UPDATE ltc_orders SET status='PAID', key=? WHERE order_id=?", (key, order_id))
                    user = await client.fetch_user(user_id)
                    await user.send(INSTRUCTIONS.format(key=key, bot=TG_BOT))
                    log.info("order %s paid, key sent", order_id)
                elif now >= expires:
                    with db() as conn:
                        conn.execute("UPDATE ltc_orders SET status='EXPIRED' WHERE order_id=?", (order_id,))
        except Exception:
            log.exception("settle loop failed")
        await asyncio.sleep(CHECK_EVERY)


class PlanSelect(ui.Select):
    def __init__(self):
        super().__init__(placeholder="Choose a plan", min_values=1, max_values=1, options=[
            discord.SelectOption(label="30 days · €5", value="30"),
            discord.SelectOption(label="90 days · €13", value="90"),
            discord.SelectOption(label="365 days · €48", value="365"),
        ])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        order = await asyncio.to_thread(create_order, interaction.user.id, self.values[0])
        await interaction.followup.send(
            f"🧾 **Order {order['order_id']}**\n"
            f"Plan: {order['days']} days · €{order['eur']}\n\n"
            f"Send **{order['amount']} LTC** to:\n`{order['address']}`\n\n"
            "Only send Litecoin (LTC) to this address. The invoice expires in 60 minutes. "
            "Your key arrives by DM as soon as the payment is confirmed.",
            ephemeral=True)


class PlanView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(PlanSelect())


class BuyView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="💳 Buy access", style=discord.ButtonStyle.primary, custom_id="adeleai_buy")
    async def buy(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Choose a plan:", view=PlanView(), ephemeral=True)


intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def setup_hook():
    client.add_view(BuyView())


@client.event
async def on_ready():
    log.info("logged in as %s", client.user)
    if os.environ.get("POST_BUY"):
        channel = client.get_channel(SALES_CHANNEL_ID) or await client.fetch_channel(SALES_CHANNEL_ID)
        await channel.send(
            "🔓 **AdeleAI Private**\n"
            "Access to the private group with top AI unlocks.\n\n"
            "**Plans:** 30 days €5 · 90 days €13 · 365 days €48\n"
            "**Pay:** Litecoin (LTC) only\n\n"
            "Press the button below to get your invoice.",
            view=BuyView())
        log.info("buy message posted")
        await client.close()
        return
    await client.change_presence(activity=discord.Game(name="🔓 Jailbreaking top AI models"))
    client.loop.create_task(settle_orders(client))


if __name__ == "__main__":
    init_db()
    client.run(TOKEN, log_handler=None)
    sys.exit(0)
