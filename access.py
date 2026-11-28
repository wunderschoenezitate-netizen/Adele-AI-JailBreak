"""Single owner of subscriptions, orders and access keys.

The Telegram bot, the Discord bot and the website all create orders and grant access through this module.
Access itself is the private Telegram group: a paid order either extends the buyer's Telegram subscription
directly (Telegram orders) or issues a single-use ADL key (website and Discord orders) that the buyer redeems
in the Telegram bot. That key is the link between a website/Discord purchase and a Telegram identity.
"""
import json
import logging
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation

import payment_config as cfg

log = logging.getLogger("access")

KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
KEY_RE = re.compile(r"ADL-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}")
ORDER_ID_RE = re.compile(r"AD-[0-9A-F]{16}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32}")
CHANNELS = ("web", "discord", "telegram")


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db(db):
    # subscriptions and keys keep the exact columns bot.py and discord_sales.py already create.
    db.execute("CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY, days INTEGER NOT NULL, order_id TEXT, used_by INTEGER, used_at INTEGER)")
    db.execute("""CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, channel TEXT NOT NULL,
        tg_user INTEGER, discord_user INTEGER, plan TEXT NOT NULL, days INTEGER NOT NULL, eur TEXT NOT NULL,
        crypto TEXT NOT NULL, address TEXT, amount_crypto TEXT, status TEXT NOT NULL,
        created INTEGER NOT NULL, expires INTEGER NOT NULL, paid_at INTEGER, key TEXT,
        notified INTEGER NOT NULL DEFAULT 0, last_callback TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS orders_notify ON orders (status, notified)")
    # users has the same columns bot.py creates; referral_* tables belong to the affiliate reward.
    db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, referrer_id INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS referral_qualified (user_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, at INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS referral_qualified_referrer ON referral_qualified (referrer_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS referral_rewards (
        referrer_id INTEGER NOT NULL, unit INTEGER NOT NULL, days INTEGER NOT NULL, created INTEGER NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (referrer_id, unit))""")
    db.commit()


def new_key():
    return "ADL-" + "-".join("".join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(3))


def extend_subscription(db, user, days, now=None):
    """Extend inside the caller's transaction. Returns the new expiry."""
    now = int(time.time()) if now is None else now
    row = db.execute("SELECT expires FROM subscriptions WHERE user_id=?", (user,)).fetchone()
    expires = max(now, row[0] if row else 0) + days * 86400
    db.execute("INSERT INTO subscriptions VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires",
               (user, expires))
    return expires


def create_order(db, channel, plan, crypto, tg_user=None, discord_user=None, invoice=None, now=None):
    """Create an order and its Shkeeper invoice. invoice(crypto, order_id, eur) -> (address, amount).

    The row is written before the invoice is requested, so a callback can never arrive for an unknown order.
    """
    if channel not in CHANNELS or plan not in cfg.PLANS:
        raise ValueError("invalid channel or plan")
    if crypto not in cfg.COINS:
        raise ValueError("coin not enabled")
    if invoice is None:
        import shkeeper_api
        invoice = shkeeper_api.create_invoice
    eur, days = cfg.PLANS[plan]
    now = int(time.time()) if now is None else now
    order_id = "AD-" + secrets.token_hex(8).upper()
    token = secrets.token_urlsafe(24)
    with db:
        db.execute("INSERT INTO orders (order_id, token, channel, tg_user, discord_user, plan, days, eur, crypto,"
                   " status, created, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)",
                   (order_id, token, channel, tg_user, discord_user, plan, days, eur, crypto, now, now + cfg.ORDER_TTL))
    try:
        address, amount = invoice(crypto, order_id, eur)
    except Exception:
        with db:
            db.execute("UPDATE orders SET status='FAILED' WHERE order_id=? AND status='NEW'", (order_id,))
        raise
    with db:
        db.execute("UPDATE orders SET address=?, amount_crypto=?, status='PENDING' WHERE order_id=? AND status='NEW'",
                   (address, amount, order_id))
    return order(db, order_id)


ORDER_COLUMNS = ("order_id", "token", "channel", "tg_user", "discord_user", "plan", "days", "eur", "crypto",
                 "address", "amount_crypto", "status", "created", "expires", "paid_at", "key", "notified")


def order(db, order_id):
    row = db.execute(f"SELECT {', '.join(ORDER_COLUMNS)} FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return dict(zip(ORDER_COLUMNS, row)) if row else None


def order_by_token(db, token):
    if not TOKEN_RE.fullmatch(token or ""):
        return None
    row = db.execute("SELECT order_id FROM orders WHERE token=?", (token,)).fetchone()
    return order(db, row[0]) if row else None


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def apply_callback(db, payload, now=None):
    """Apply one verified Shkeeper callback. Returns a short outcome string for logging.

    Every outcome is acknowledged with 202 by the caller: a valid signature means Shkeeper sent it, and
    any non-202 answer makes Shkeeper resend it every 60 s forever. Outcomes that need a human are logged
    and stored on the order instead.
    """
    now = int(time.time()) if now is None else now
    if not isinstance(payload, dict):
        return "ignored: payload is not an object"
    external_id = payload.get("external_id")
    if not isinstance(external_id, str) or not ORDER_ID_RE.fullmatch(external_id):
        return "ignored: foreign or malformed external_id"
    current = order(db, external_id)
    if current is None:
        return "ignored: unknown order"
    db.execute("UPDATE orders SET last_callback=? WHERE order_id=?", (json.dumps(payload)[:4000], external_id))
    db.commit()

    status = payload.get("status")
    if status == "unconfirmed":
        return "seen unconfirmed transaction"
    if status not in ("PARTIAL", "PAID", "OVERPAID"):
        return f"ignored: status {status!r}"
    if current["status"] == "PAID":
        return "duplicate: already paid"
    if payload.get("fiat") != cfg.FIAT:
        _flag(db, external_id, "REVIEW")
        log.error("Order %s: callback fiat %r, expected %s", external_id, payload.get("fiat"), cfg.FIAT)
        return "review: fiat mismatch"
    balance = _decimal(payload.get("balance_fiat"))
    price = Decimal(current["eur"])
    if balance is None or balance < 0:
        _flag(db, external_id, "REVIEW")
        return "review: invalid balance_fiat"
    if status == "PARTIAL" or payload.get("paid") is not True:
        if current["status"] in ("NEW", "PENDING"):
            _flag(db, external_id, "PARTIAL")
        return f"partial: {balance} of {price} {cfg.FIAT}"
    if balance < price * cfg.SHKEEPER_MIN_PAID_RATIO:
        _flag(db, external_id, "REVIEW")
        log.error("Order %s: Shkeeper says %s but balance %s < %s x %s", external_id, status, balance, price,
                  cfg.SHKEEPER_MIN_PAID_RATIO)
        return "review: paid status below minimum ratio"
    return "paid" if fulfil(db, external_id, now) else "duplicate: already paid"


def _flag(db, order_id, status):
    with db:
        db.execute("UPDATE orders SET status=? WHERE order_id=? AND status!='PAID'", (status, order_id))


def fulfil(db, order_id, now=None):
    """Mark the order paid and grant access exactly once. Returns False when it was already paid."""
    now = int(time.time()) if now is None else now
    with db:
        cur = db.execute("UPDATE orders SET status='PAID', paid_at=? WHERE order_id=? AND status!='PAID'",
                         (now, order_id))
        if cur.rowcount != 1:
            return False
        tg_user, days = db.execute("SELECT tg_user, days FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if tg_user is not None:
            extend_subscription(db, tg_user, days, now)
            referral_qualify(db, tg_user, now)
        else:
            key = new_key()
            db.execute("INSERT INTO keys (key, days, order_id) VALUES (?, ?, ?)", (key, days, order_id))
            db.execute("UPDATE orders SET key=? WHERE order_id=?", (key, order_id))
    return True


def redeem_key(db, user, key, now=None):
    """Return an error message, or None when the key was accepted."""
    row = db.execute("SELECT days, used_by FROM keys WHERE key=?", (key,)).fetchone()
    if not row:
        return "❌ Key not found. Check the key and try again."
    if row[1] is not None:
        return "❌ This key was already used."
    now = int(time.time()) if now is None else now
    with db:
        cur = db.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=? AND used_by IS NULL", (user, now, key))
        if cur.rowcount != 1:
            return "❌ This key was already used."
        extend_subscription(db, user, row[0], now)
        referral_qualify(db, user, now)
    return None


def pending_notifications(db, channel_column):
    """Paid orders whose buyer on the given channel has not been told yet."""
    if channel_column not in ("tg_user", "discord_user"):
        raise ValueError(channel_column)
    return db.execute(f"SELECT order_id, {channel_column}, key, days FROM orders"
                      f" WHERE status='PAID' AND notified=0 AND {channel_column} IS NOT NULL").fetchall()


def mark_notified(db, order_id):
    with db:
        db.execute("UPDATE orders SET notified=1 WHERE order_id=?", (order_id,))


def referral_qualify(db, user, now=None):
    """Count `user` as a subscribed member of their referrer and grant free days for each full group of members.

    Call it inside the transaction that grants the user's subscription through a purchase or a redeemed key (not for
    reward days). A user counts once, and each reward step (referrer, unit) is inserted once, so repeated or
    concurrent calls never grant twice. Returns the number of reward steps granted now.
    """
    now = int(time.time()) if now is None else now
    row = db.execute("SELECT referrer_id FROM users WHERE user_id=?", (user,)).fetchone()
    referrer = row[0] if row else None
    if referrer is None or referrer == user:
        return 0
    cur = db.execute("INSERT OR IGNORE INTO referral_qualified (user_id, referrer_id, at) VALUES (?, ?, ?)",
                     (user, referrer, now))
    if cur.rowcount != 1:
        return 0
    qualified = db.execute("SELECT COUNT(*) FROM referral_qualified WHERE referrer_id=?", (referrer,)).fetchone()[0]
    granted = 0
    for unit in range(1, qualified // cfg.REFERRAL_INVITES + 1):
        cur = db.execute("INSERT OR IGNORE INTO referral_rewards (referrer_id, unit, days, created) VALUES (?, ?, ?, ?)",
                         (referrer, unit, cfg.REFERRAL_REWARD_DAYS, now))
        if cur.rowcount == 1:
            extend_subscription(db, referrer, cfg.REFERRAL_REWARD_DAYS, now)
            granted += 1
    return granted


def referral_stats(db, user):
    """(people who started the bot through the link, subscribed members through the link)"""
    started = db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user,)).fetchone()[0]
    subscribed = db.execute("SELECT COUNT(*) FROM referral_qualified WHERE referrer_id=?", (user,)).fetchone()[0]
    return started, subscribed


def pending_referral_rewards(db):
    return db.execute("SELECT referrer_id, unit, days FROM referral_rewards WHERE notified=0").fetchall()


def mark_referral_notified(db, referrer, unit):
    with db:
        db.execute("UPDATE referral_rewards SET notified=1 WHERE referrer_id=? AND unit=?", (referrer, unit))
