"""Payment configuration shared by the Telegram bot, the Discord bot and the website.

Provenance of the Shkeeper values below, measured from the Shkeeper source:
  repo https://github.com/vsys-host/shkeeper.io  commit e6a41284cf36c3837a155339912480c4371e7b63 (2026-08-31)
  helm https://github.com/vsys-host/helm-charts   commit 05558511cd595b2ef3bc92f04d4141eeb80487ae

FROM DOCS/SOURCE (README sections 5.2 and 5.3, shkeeper/api_v1.py, shkeeper/callback.py,
shkeeper/services/webhook_hmac.py, shkeeper/modules/cryptos/*.py):
  - Invoice endpoint: POST {base}/api/v1/{crypto}/payment_request, header X-Shkeeper-Api-Key,
    JSON {external_id, fiat, amount, callback_url}. Errors come back as HTTP 200 with status "error".
  - Success response fields: amount (crypto), wallet (address), exchange_rate, recalculate_after, display_name.
  - Coin list: GET {base}/api/v1/crypto (no auth), use crypto_list[].name.
  - Every wallet has its own api key (shkeeper/models.py Wallet.apikey); api_key_required accepts any wallet's key,
    and callbacks are signed with the key of the crypto's wallet (shkeeper/callback.py).
  - Callback signature: hex HMAC-SHA256(api_key, "{X-Shkeeper-Timestamp}." + raw body), header X-Shkeeper-Signature,
    recommended max age 300 s. Only HTTP 202 stops the 60 s resend loop.
  - Callback kinds: an "unconfirmed" notice {status: "unconfirmed", external_id, crypto, addr, txid, amount},
    and invoice notices with status PARTIAL / PAID / OVERPAID, paid (bool), fiat, balance_fiat, transactions.
    A notice is sent for every transaction, also after the invoice is already PAID.
  - Shkeeper marks an invoice PAID at llimit percent of the fiat amount; the wallet default is 95.
  - Crypto names: BTC, LTC, ETH, SOL, USDT (= TRC20 USDT), ETH-USDT (ERC20), SOLANA-USDT (SPL).
  - Helm defaults: BTC and LTC point to the vendor's public nodes (fullnode.bitcoin/litecoin.shkeeper.io);
    ETH and SOL need an RPC URL you provide (eth_fullnode.url, solana_fullnode.url).

ASSUMPTIONS (not confirmed by the docs, change if your setup differs):
  - USDT is offered as ETH-USDT and SOLANA-USDT because those reuse the ETH and SOL backends that are needed
    anyway. TRC20 "USDT" needs a Tron node (tron_fullnode) and is not in the default list.
  - SHKEEPER_MIN_PAID_RATIO mirrors the Shkeeper default llimit (95 %). If you change llimit in the
    Shkeeper wallet settings, change this value too.
"""
import os
from decimal import Decimal

# Plan id -> (price in EUR as a decimal string, days of access). Same values as bot.py and discord_sales.py.
PLANS = {"30": ("5", 30), "90": ("13", 90), "365": ("48", 365)}
FIAT = "EUR"

SHKEEPER_URL = os.environ.get("SHKEEPER_URL", "").rstrip("/")      # e.g. https://pay.adeleai.org
# One key, or several separated by commas (each Shkeeper wallet has its own key and signs its callbacks with it).
# The first key authenticates API calls (Shkeeper accepts any wallet key); all keys are accepted for callbacks.
SHKEEPER_API_KEYS = [k.strip() for k in os.environ.get("SHKEEPER_API_KEY", "").split(",") if k.strip()]
SHKEEPER_API_KEY = SHKEEPER_API_KEYS[0] if SHKEEPER_API_KEYS else ""
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://adeleai.org").rstrip("/")
# Affiliate reward: every REFERRAL_INVITES subscribed members who joined through a user's referral link give that
# user REFERRAL_REWARD_DAYS days of free access.
REFERRAL_INVITES = max(1, int(os.environ.get("REFERRAL_INVITES", "10")))
REFERRAL_REWARD_DAYS = max(1, int(os.environ.get("REFERRAL_REWARD_DAYS", "7")))
CALLBACK_PATH = "/shkeeper/callback"
CALLBACK_MAX_AGE = 300

# Shkeeper crypto name -> label shown to buyers. Order is the display order.
COIN_LABELS = {
    "BTC": "Bitcoin (BTC)",
    "LTC": "Litecoin (LTC)",
    "ETH": "Ethereum (ETH)",
    "SOL": "Solana (SOL)",
    "SOLANA-USDT": "USDT on Solana",
    "ETH-USDT": "USDT on Ethereum (ERC20)",
    "USDT": "USDT on Tron (TRC20)",
}
DEFAULT_COINS = "BTC,LTC,ETH,SOL,SOLANA-USDT,ETH-USDT"
COINS = [c.strip().upper() for c in os.environ.get("SHKEEPER_COINS", DEFAULT_COINS).split(",") if c.strip()]

SHKEEPER_MIN_PAID_RATIO = Decimal(os.environ.get("SHKEEPER_MIN_PAID_RATIO", "0.95"))
ORDER_TTL = 3600  # how long the status page presents an invoice as current; late payments are still honoured


def shkeeper_enabled():
    return bool(SHKEEPER_URL and SHKEEPER_API_KEY)


def callback_url():
    return PUBLIC_BASE_URL + CALLBACK_PATH
