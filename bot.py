import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import http.client
import json
import socket
from urllib.request import Request, urlopen

TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = os.environ["PRIVATE_CHANNEL_ID"]
PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL_ID", "")
PRIVATE_TOPIC_ID = int(os.environ.get("PRIVATE_TOPIC_ID", "0")) or None  # forum topic in the private group
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")  # public posts are mirrored here
DISCORD_UA = "AdeleAIBot/1.0"
PRICE = int(os.environ.get("PRICE_STARS", "500"))
CRYPTO_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "")
PLANS = {"30": ("5", 30), "90": ("13", 90), "365": ("48", 365)}
DB = os.environ.get("DATABASE_PATH", "adeleai.sqlite3")
logging.basicConfig(level=logging.INFO)


if os.environ.get("FORCE_IPV4", "1") != "0":
    # Python tries every resolved address in turn with the full timeout, so one blackholed IPv6 route costs 20-40 s.
    _getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = lambda host, port, family=0, *args, **kwargs: _getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)

SLOW_CALL_SECONDS = 2.0
_local = threading.local()


def api(method, **data):
    # One kept-alive HTTPS connection per thread: no TLS handshake per call.
    body = json.dumps(data).encode("utf-8")
    timeout = data.get("timeout", 0) + 10 if method == "getUpdates" else 20
    started = time.monotonic()
    for attempt in (0, 1):
        conn = getattr(_local, "conn", None)
        reused = conn is not None
        if conn is None:
            conn = _local.conn = http.client.HTTPSConnection("api.telegram.org", timeout=timeout)
        elif conn.sock:
            conn.sock.settimeout(timeout)
        try:
            conn.request("POST", f"/bot{TOKEN}/{method}", body, {"Content-Type": "application/json"})
            raw = conn.getresponse().read()
            break
        except Exception as exc:
            conn.close()
            _local.conn = None
            # A connection the server closed while idle fails immediately; retry once on a fresh one.
            if reused and attempt == 0 and isinstance(exc, ConnectionError):
                continue
            raise
    elapsed = time.monotonic() - started
    if elapsed > SLOW_CALL_SECONDS and method != "getUpdates":
        logging.warning("Slow Telegram call %s: %.1f s", method, elapsed)
    result = json.loads(raw)
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
            send(user, "Crypto payments are being set up. Please tap 🆘 Support to open a ticket.")
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
        send(user, "AdeleAI Private gives subscribers access to the private group.\n\nPlans: 30 days €5, 90 days €13, 365 days €48. Crypto plans require a new payment to renew.\n\nPayment support: tap 🆘 Support in the menu to open a ticket.")


def invite(chat):
    link = api("createChatInviteLink", chat_id=CHANNEL, member_limit=1,
               expire_date=int(time.time()) + 3600)["invite_link"]
    send(chat, "Your subscription is active. This single-use invitation expires in one hour:\n" + link)


MEDIA_KEYS = ("photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker")


def has_media(message):
    return any(key in message for key in MEDIA_KEYS)


def discord_escape(chunk):
    # Only escape characters Discord would otherwise turn into formatting.
    return re.sub(r"([\*_~`|])", r"\\1", chunk)


def discord_render(chunk, depth):
    text = chunk if (depth["code"] or depth["raw"]) else discord_escape(chunk)
    if depth["quote"]:
        text = text.replace("\n", "\n> ")
    return text


def telegram_to_discord_md(text, entities):
    """Convert Telegram text + entities (UTF-16 offsets) to Discord markdown."""
    units = text.encode("utf-16-le")
    total = len(units) // 2

    def slice_units(a, b):
        return units[a * 2:b * 2].decode("utf-16-le")

    simple = {"bold": ("**", "**"), "italic": ("*", "*"), "underline": ("__", "__"),
              "strikethrough": ("~~", "~~"), "spoiler": ("||", "||")}
    events = []  # (pos, rank, sort_key, open_marker, close_marker, mode)
    for e in entities or []:
        start, end = e["offset"], e["offset"] + e["length"]
        kind = e["type"]
        if kind == "text_link":
            o, c, mode = "[", f"]({e['url']})", None
        elif kind == "mention":
            handle = slice_units(start, end).lstrip("@")
            o, c, mode = "[", f"](https://t.me/{handle})", "raw"
        elif kind in simple:
            (o, c), mode = simple[kind], None
        elif kind == "code":
            o, c, mode = "`", "`", "code"
        elif kind == "pre":
            o, c, mode = "```\n", "\n```", "code"
        elif kind in ("blockquote", "expandable_blockquote"):
            o, c, mode = "> ", "", "quote"
        else:
            continue
        events.append((start, 1, -end, o, mode, True))
        events.append((end, 0, -start, c, mode, False))

    out, cursor = [], 0
    depth = {"code": 0, "raw": 0, "quote": 0}
    for pos, _, _, marker, mode, opening in sorted(events, key=lambda ev: (ev[0], ev[1], ev[2])):
        out.append(discord_render(slice_units(cursor, pos), depth))
        out.append(marker)
        cursor = pos
        if mode:
            depth[mode] += 1 if opening else -1
    out.append(discord_render(slice_units(cursor, total), depth))
    return "".join(out)


def discord_post(body, content_type):
    req = Request(DISCORD_WEBHOOK, data=body, headers={"Content-Type": content_type, "User-Agent": DISCORD_UA})
    with urlopen(req, timeout=30) as response:
        response.read()


def media_info(message):
    for key in MEDIA_KEYS:
        if key in message:
            item = message[key][-1] if key == "photo" else message[key]
            name = item.get("file_name") or ("photo.jpg" if key == "photo" else f"{key}")
            return item["file_id"], name, item.get("file_size")
    return None


def mirror_to_discord(body_text, entities, media_message=None):
    if not DISCORD_WEBHOOK:
        return
    md = telegram_to_discord_md(body_text or "", entities or [])[:4000]
    payload = {"username": "Adele AI"}
    if md:
        payload["embeds"] = [{"description": md, "color": 0x0EA5E9}]
    info = media_info(media_message) if media_message else None
    if info is None or (info[2] or 0) > 9_000_000:
        if "embeds" in payload:
            discord_post(json.dumps(payload).encode("utf-8"), "application/json")
        return
    file_id, filename, _ = info
    file_path = api("getFile", file_id=file_id)["file_path"]
    with urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=60) as response:
        data = response.read()
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n".encode("utf-8"),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"),
        data, b"\r\n", f"--{boundary}--\r\n".encode("utf-8"),
    ])
    discord_post(body, f"multipart/form-data; boundary={boundary}")


KEY_RE = re.compile(r"ADL-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}")


def redeem_key(user, key, db):
    """Return an error message, or None when the key was accepted."""
    row = db.execute("SELECT days, used_by FROM keys WHERE key=?", (key,)).fetchone()
    if not row:
        return "❌ Key not found. Check the key and try again."
    if row[1] is not None:
        return "❌ This key was already used."
    now = int(time.time())
    with db:
        cur = db.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=? AND used_by IS NULL", (user, now, key))
        if cur.rowcount != 1:
            return "❌ This key was already used."
        existing = db.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user,)).fetchone()
        expires = max(now, existing[0] if existing else 0) + row[0] * 86400
        db.execute("INSERT INTO subscriptions VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires",
                   (user, expires))
    return None


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
    if message.get("new_chat_members") and str(message.get("chat", {}).get("id")) == str(CHANNEL):
        # Telegram's "X joined the group" service message in the private group.
        try:
            api("deleteMessage", chat_id=message["chat"]["id"], message_id=message["message_id"])
        except Exception:
            logging.exception("Could not delete join message %s", message.get("message_id"))
        return
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
    key_candidate = text.strip().upper()
    if KEY_RE.fullmatch(key_candidate):
        error = redeem_key(user, key_candidate, db)
        if error:
            send(user, error)
        else:
            send(user, "✅ Key accepted. Your access is active.")
            invite(user)
        return
    if user == admin_id and text == "/open":
        db.execute("INSERT OR REPLACE INTO admin_post_mode VALUES (?)", (user,))
        db.commit()
        send(user, "📝 Post mode on. Your next message (text, photo or file) goes to the public channel and Discord. /cancel to stop.")
        return
    is_post_command = text.startswith("/") and not text.startswith(("/public ", "/private "))
    in_post_mode = db.execute("SELECT 1 FROM admin_post_mode WHERE user_id=?", (user,)).fetchone()
    if user == admin_id and in_post_mode and not message.get("reply_to_message") and not is_post_command and (text or has_media(message)):
        # Default target is the public jailbreak channel. Start a text or caption with "/private " for the private channel.
        media = has_media(message)
        body = message.get("caption", "") if media else text
        entities_key = "caption_entities" if media else "entities"
        entities = message.get(entities_key) or []
        target, name, cut, thread = PUBLIC_CHANNEL, "public channel", 0, None
        for prefix, prefix_target, prefix_name in (("/private ", CHANNEL, "private channel"), ("/public ", PUBLIC_CHANNEL, "public channel")):
            if body.startswith(prefix):
                target, name, cut = prefix_target, prefix_name, len(prefix)
                thread = PRIVATE_TOPIC_ID if prefix_target == CHANNEL else None
                break
        if not target:
            send(user, "No target channel is configured.")
            return
        new_body = body[cut:]
        # Shift formatting entities so bold, links, etc. survive the prefix removal.
        new_entities = [{**e, "offset": e["offset"] - cut} for e in entities if e["offset"] >= cut] if cut else entities
        try:
            if media:
                params = {"chat_id": target, "from_chat_id": user, "message_id": message["message_id"]}
                if thread:
                    params["message_thread_id"] = thread
                if cut:
                    params["caption"] = new_body
                    params["caption_entities"] = new_entities
                api("copyMessage", **params)
            elif cut:
                if not new_body:
                    send(user, "Nothing to post.")
                    return
                api("sendMessage", chat_id=target, text=new_body, entities=new_entities,
                    **({"message_thread_id": thread} if thread else {}))
            else:
                api("copyMessage", chat_id=target, from_chat_id=user, message_id=message["message_id"],
                    **({"message_thread_id": thread} if thread else {}))
            db.execute("DELETE FROM admin_post_mode WHERE user_id=?", (user,))
            db.commit()
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
        db.execute("DELETE FROM admin_post_mode WHERE user_id=?", (user,))
        db.commit()
        send(user, "Cancelled.")
        return
    if db.execute("SELECT 1 FROM support_pending WHERE user_id=?", (user,)).fetchone() and text:
        if not admin_id:
            send(user, "Support is currently unavailable. Please try again later.")
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
        send(user, "For payment support, tap 🆘 Support in the menu to open a ticket.")
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
    db.execute("CREATE TABLE IF NOT EXISTS admin_post_mode (user_id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, days INTEGER NOT NULL, order_id TEXT, used_by INTEGER, used_at INTEGER)")
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

