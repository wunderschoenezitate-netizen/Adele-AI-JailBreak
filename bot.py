import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import json
from urllib.request import Request, urlopen

TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ["PRIVATE_CHANNEL_ID"]
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL_ID", "")
PRICE = int(os.environ.get("PRICE_STARS", "500"))
CRYPTO_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "")
PLANS = {"30": ("5", 30), "90": ("13", 90), "365": ("48", 365)}
API = f"https://api.telegram.org/bot{TOKEN}/"
DB = os.environ.get("DATABASE_PATH", "adeleai.sqlite3")
logging.basicConfig(level=logging.INFO)


def api(method, **data):
    request = Request(API + method, data=json.dumps(data).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=40) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"{method}: {result}")
    return result.get("result")


def send(chat, text, markup=None):
    data = {"chat_id": chat, "text": text}
    if markup is not None:
        data["reply_markup"] = markup
    api("sendMessage", **data)


def crypto_api(method, **data):
    request = Request("https://pay.crypt.bot/api/" + method,
                      data=json.dumps(data).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Crypto-Pay-API-Token": CRYPTO_TOKEN,
                               "User-Agent": "AdeleAIPrivateBot/1.0"})
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"Crypto Pay {method}: {result.get('error')}")
    return result["result"]


def menu(chat):
    send(chat, "AdeleAI Private 🔒 — everything you need to use AI without limits.\n\nInside:\n🔓 Jailbreaks for all top models\n🆓 Ways to get free access to paid AI\n⚡ Working prompts and workflows\n📢 Everything before anyone else\n\nChoose an action 👇", {
        "keyboard": [[{"text": "💳 Subscription"}, {"text": "📊 My status"}],
                     [{"text": "🔗 Referral link"}, {"text": "📋 Information"}],
                     [{"text": "🆘 Support"}]],
        "resize_keyboard": True})


def action(name, user, db):
    row = db.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user,)).fetchone()
    active = bool(row and row[0] > time.time())
    if name == "buy":
        send(user, "AdeleAI Private plans\n\n30 days — €5\n90 days — €13\n365 days — €48\n\nPay with USDT, ETH, BTC or LTC. Each purchase grants access for the selected period.", {
            "inline_keyboard": [[{"text": "30 days · €5", "callback_data": "plan_30"}],
                                [{"text": "90 days · €13", "callback_data": "plan_90"}],
                                [{"text": "365 days · €48", "callback_data": "plan_365"}]]})
    elif name.startswith("plan_") and name[5:] in PLANS:
        if not CRYPTO_TOKEN:
            send(user, "Crypto payments are being set up. Please contact @Lunafromadele.")
            return
        price, days = PLANS[name[5:]]
        invoice = crypto_api("createInvoice", currency_type="fiat", fiat="EUR", amount=price,
                             accepted_assets="USDT,ETH,BTC,LTC", description=f"AdeleAI Private — {days} days",
                             payload=f"{user}:{days}", expires_in=3600)
        db.execute("INSERT INTO crypto_invoices (invoice_id, user_id, days, amount) VALUES (?, ?, ?, ?)",
                   (invoice["invoice_id"], user, days, price))
        db.commit()
        send(user, f"Pay €{price} for {days} days. This invoice expires in one hour.", {
            "inline_keyboard": [[{"text": "Pay with Crypto Bot", "url": invoice["bot_invoice_url"]}]]})
    elif name == "status":
        if active:
            expiry = datetime.fromtimestamp(row[0], timezone.utc).strftime("%d %b %Y, %H:%M UTC")
            send(user, f"✅ Your subscription is active until {expiry}.")
            invite(user)
        else:
            send(user, "You don't have an active subscription. Tap Subscription to join.")
    elif name == "referral":
        count = db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user,)).fetchone()[0]
        username = api("getMe")["username"]
        send(user, f"Your referral link:\nhttps://t.me/{username}?start=ref_{user}\n\nPeople joined through your link: {count}")
    elif name == "info":
        send(user, "AdeleAI Private gives subscribers access to the private group.\n\nPlans: 30 days €5, 90 days €13, 365 days €48. Crypto plans require a new payment to renew.\n\nPayment support: @Lunafromadele")


def invite(chat):
    link = api("createChatInviteLink", chat_id=CHANNEL, member_limit=1,
               expire_date=int(time.time()) + 3600)["invite_link"]
    send(chat, "Your subscription is active. This single-use invitation expires in one hour:\n" + link)


MEDIA_KEYS = ("photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker")


def has_media(message):
    return any(key in message for key in MEDIA_KEYS)


def handle(update, db):
    if "pre_checkout_query" in update:
        q = update["pre_checkout_query"]
        valid = q.get("invoice_payload") == "adeleai-monthly-v1" and q.get("currency") == "XTR" and q.get("total_amount") == PRICE
        args = {"pre_checkout_query_id": q["id"], "ok": valid}
        if not valid:
            args["error_message"] = "This invoice is no longer valid. Please start again."
        api("answerPreCheckoutQuery", **args)
        return
    if "callback_query" in update:
        q = update["callback_query"]
        api("answerCallbackQuery", callback_query_id=q["id"])
        user = q["from"]["id"]
        action(q["data"], user, db)
        return
    message = update.get("message", {})
    if message.get("chat", {}).get("type") != "private":
        return
    user = message["from"]["id"]
    text = message.get("text", "")
    admin = db.execute("SELECT value FROM settings WHERE key='admin_id'").fetchone()
    admin_id = int(admin[0]) if admin else None
    if user == admin_id and text.startswith("/post "):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send(user, "Usage: /post @channel Your message")
            return
        target, content = parts[1], parts[2]
        try:
            member = api("getChatMember", chat_id=target, user_id=user)
            if member.get("status") not in ("creator", "administrator"):
                send(user, "You must be an administrator of the target channel.")
                return
            send(target, content)
            send(user, "Message published.")
        except Exception:
            logging.exception("Channel post failed")
            send(user, "Could not publish. Check the channel name and the bot's posting permissions.")
        return
    if user == admin_id and message.get("reply_to_message") and text:
        reply_id = message["reply_to_message"]["message_id"]
        ticket = db.execute("SELECT user_id FROM support_messages WHERE message_id=?", (reply_id,)).fetchone()
        if ticket:
            send(ticket[0], "💬 Support: " + text)
            send(user, "Reply sent.")
            return
    is_post_command = text.startswith("/") and not text.startswith(("/public ", "/private "))
    if user == admin_id and not message.get("reply_to_message") and not is_post_command and (text or has_media(message)):
        # Default target is the public jailbreak channel. Start a text or caption with "/private " for the private channel.
        caption = message.get("caption") or text or ""
        target, name = PUBLIC_CHANNEL, "public channel"
        if caption.startswith("/private "):
            target, name, caption = CHANNEL, "private channel", caption[len("/private "):].strip()
        elif caption.startswith("/public "):
            caption = caption[len("/public "):].strip()
        if not target:
            send(user, "No target channel is configured.")
            return
        try:
            if has_media(message):
                params = {"chat_id": target, "from_chat_id": user, "message_id": message["message_id"]}
                if caption:
                    params["caption"] = caption
                else:
                    params.pop("caption", None)
                api("copyMessage", **params)
            else:
                if not caption:
                    send(user, "Nothing to post.")
                    return
                send(target, caption)
            send(user, f"✅ Posted to the {name}.")
        except Exception:
            logging.exception("Channel post failed")
            send(user, f"Could not post to the {name}. Check the bot's posting permissions.")
        return
    if text == "🆘 Support" or text == "/support":
        db.execute("INSERT OR REPLACE INTO support_pending VALUES (?)", (user,))
        db.commit()
        send(user, "Send your support message here. Use /cancel to cancel.")
        return
    if text == "/cancel":
        db.execute("DELETE FROM support_pending WHERE user_id=?", (user,))
        db.commit()
        send(user, "Cancelled.")
        return
    if db.execute("SELECT 1 FROM support_pending WHERE user_id=?", (user,)).fetchone() and text:
        if not admin_id:
            send(user, "Support is currently unavailable. Contact @Lunafromadele.")
            return
        result = api("sendMessage", chat_id=admin_id, text=f"Support request from user {user}:\n\n{text}\n\nReply to this message to respond.")
        db.execute("INSERT INTO support_messages VALUES (?, ?)", (result["message_id"], user))
        db.execute("DELETE FROM support_pending WHERE user_id=?", (user,))
        db.commit()
        send(user, "Your message was sent to support.")
        return
    payment = message.get("successful_payment")
    if payment:
        if payment.get("invoice_payload") != "adeleai-monthly-v1" or payment.get("currency") != "XTR":
            logging.error("Unexpected payment from %s", user)
            return
        expires = payment.get("subscription_expiration_date", int(time.time()) + 2592000)
        db.execute("INSERT INTO subscriptions VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires=max(expires, excluded.expires)", (user, expires))
        db.commit()
        invite(user)
    elif message.get("text", "").split(" ")[0].split("@")[0] == "/start":
        parts = message.get("text", "").split(maxsplit=1)
        referrer = None
        if len(parts) == 2 and parts[1].startswith("ref_") and parts[1][4:].isdigit():
            candidate = int(parts[1][4:])
            if candidate != user and db.execute("SELECT 1 FROM users WHERE user_id=?", (candidate,)).fetchone():
                referrer = candidate
        db.execute("INSERT OR IGNORE INTO users (user_id, referrer_id) VALUES (?, ?)", (user, referrer))
        db.commit()
        menu(user)
    elif message.get("text", "").split(" ")[0].split("@")[0] == "/paysupport":
        send(user, "For payment support, contact @Lunafromadele.")
    else:
        choices = {"💳 Subscription": "buy", "📊 My status": "status", "🔗 Referral link": "referral", "📋 Information": "info"}
        selected = choices.get(message.get("text"))
        if selected:
            action(selected, user, db)


def expire_members(db):
    for user, in db.execute("SELECT user_id FROM subscriptions WHERE expires <= ?", (int(time.time()),)).fetchall():
        try:
            api("banChatMember", chat_id=CHANNEL, user_id=user)
            api("unbanChatMember", chat_id=CHANNEL, user_id=user, only_if_banned=True)
            db.execute("DELETE FROM subscriptions WHERE user_id=?", (user,))
            db.commit()
        except Exception:
            logging.exception("Could not remove expired subscriber %s", user)


def check_crypto_payments(db):
    if not CRYPTO_TOKEN:
        return
    pending = db.execute("SELECT invoice_id, user_id, days, amount FROM crypto_invoices WHERE credited=0").fetchall()
    for invoice_id, user, days, amount in pending:
        invoices = crypto_api("getInvoices", invoice_ids=str(invoice_id), count=1).get("items", [])
        if not invoices:
            continue
        invoice = invoices[0]
        if invoice.get("status") == "expired":
            db.execute("DELETE FROM crypto_invoices WHERE invoice_id=?", (invoice_id,))
            db.commit()
            continue
        if invoice.get("status") != "paid":
            continue
        if invoice.get("fiat") != "EUR" or Decimal(invoice.get("amount", "0")) != Decimal(amount) or invoice.get("payload") != f"{user}:{days}":
            logging.error("Invoice %s has unexpected payment details", invoice_id)
            continue
        with db:
            row = db.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user,)).fetchone()
            expires = max(int(time.time()), row[0] if row else 0) + days * 86400
            db.execute("INSERT INTO subscriptions VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires", (user, expires))
            db.execute("UPDATE crypto_invoices SET credited=1 WHERE invoice_id=?", (invoice_id,))
        try:
            invite(user)
        except Exception:
            logging.exception("Could not send invite for invoice %s; user can use My status", invoice_id)


def connect():
    # One connection per thread. WAL lets the background checks and the handlers write without blocking each other.
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def background(interval=30):
    db = connect()
    while True:
        try:
            expire_members(db)
            check_crypto_payments(db)
        except Exception:
            logging.exception("Background check failed")
        time.sleep(interval)


def process(update):
    db = connect()
    try:
        handle(update, db)
    except Exception:
        logging.exception("Update %s failed", update.get("update_id"))
    finally:
        db.close()


def main():
    if PRICE < 1:
        raise ValueError("PRICE_STARS must be positive")
    db = connect()
    db.execute("CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, referrer_id INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS crypto_invoices (invoice_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, days INTEGER NOT NULL, amount TEXT NOT NULL, credited INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS support_pending (user_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS support_messages (message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
    db.commit()
    while True:
        try:
            admins = api("getChatAdministrators", chat_id=CHANNEL)
            creator = next((entry["user"]["id"] for entry in admins if entry["status"] == "creator"), None)
            if creator:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('admin_id', ?)", (str(creator),))
                db.commit()
            api("deleteWebhook", drop_pending_updates=False)
            break
        except Exception:
            logging.exception("Bot startup failed; retrying")
            time.sleep(5)
    threading.Thread(target=background, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=8)
    offset = 0
    while True:
        try:
            for update in api("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query", "pre_checkout_query"]):
                pool.submit(process, update)
                offset = update["update_id"] + 1
        except Exception:
            logging.exception("Polling failed")
            time.sleep(5)


if __name__ == "__main__":
    main()

