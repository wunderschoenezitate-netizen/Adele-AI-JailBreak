"""Minimal Shkeeper client: coin list, invoice creation and webhook verification.

Endpoint and field names are documented with their source in payment_config.py.
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from urllib.request import Request, urlopen

import payment_config as cfg

UA = "AdeleAIShop/1.0"
_coin_cache = {"at": 0.0, "coins": []}


class ShkeeperError(RuntimeError):
    pass


def _request(method, path, body=None, auth=True):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        headers["X-Shkeeper-Api-Key"] = cfg.SHKEEPER_API_KEY
    req = Request(cfg.SHKEEPER_URL + path, data=data, headers=headers, method=method)
    with urlopen(req, timeout=20) as response:
        result = json.loads(response.read(1_000_000))
    if not isinstance(result, dict) or result.get("status") != "success":
        message = result.get("message") if isinstance(result, dict) else "bad response"
        raise ShkeeperError(f"Shkeeper {path}: {message}")
    return result


def available_coins():
    """Configured coins that Shkeeper currently reports as online, in display order. Cached for 60 s."""
    if not cfg.shkeeper_enabled():
        return []
    now = time.monotonic()
    if now - _coin_cache["at"] < 60:
        return list(_coin_cache["coins"])
    try:
        online = {entry.get("name") for entry in _request("GET", "/api/v1/crypto", auth=False).get("crypto_list", [])
                  if isinstance(entry, dict)}
    except Exception:
        # Keep showing the last known list for a short outage rather than hiding every payment option.
        return list(_coin_cache["coins"])
    coins = [c for c in cfg.COINS if c in online and c in cfg.COIN_LABELS]
    _coin_cache.update(at=now, coins=coins)
    return list(coins)


def create_invoice(crypto, external_id, eur):
    """Return (address, crypto_amount) for a new or updated Shkeeper invoice."""
    if crypto not in cfg.COINS or crypto not in cfg.COIN_LABELS:
        raise ShkeeperError(f"coin {crypto} is not enabled")
    result = _request("POST", f"/api/v1/{quote(crypto, safe='-')}/payment_request", {
        "external_id": external_id, "fiat": cfg.FIAT, "amount": eur, "callback_url": cfg.callback_url()})
    address, amount = result.get("wallet"), result.get("amount")
    if not isinstance(address, str) or not 20 <= len(address) <= 128:
        raise ShkeeperError("Shkeeper returned no usable address")
    try:
        if Decimal(str(amount)) <= 0:
            raise ShkeeperError("Shkeeper returned a non-positive amount")
    except InvalidOperation as exc:
        raise ShkeeperError("Shkeeper returned an invalid amount") from exc
    return address, str(amount)


def verify_any(secrets, body, timestamp, signature, now=None, max_age=cfg.CALLBACK_MAX_AGE):
    """True if the signature matches any configured wallet key."""
    return any([verify_signature(s, body, timestamp, signature, now, max_age) for s in secrets])


def verify_signature(secret, body, timestamp, signature, now=None, max_age=cfg.CALLBACK_MAX_AGE):
    """HMAC-SHA256(secret, f"{timestamp}." + raw body), as in shkeeper/services/webhook_hmac.py."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    sig = signature.strip().lower()
    if len(sig) != 64:
        return False
    clock = int(time.time()) if now is None else now
    if abs(clock - ts) > max_age:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("ascii") + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
