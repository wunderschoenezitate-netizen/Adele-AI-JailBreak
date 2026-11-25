import hashlib
import hmac
import json
import os

from ecdsa import SECP256k1, VerifyingKey
from ecdsa.errors import MalformedPointError
from Crypto.Hash import RIPEMD160, keccak

XPUBS_FILE = "xpubs.json"

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
ZPUB_VERSION = bytes.fromhex("04b24746")
XPUB_VERSION = bytes.fromhex("0488b21e")
LTUB_VERSION = bytes.fromhex("019da462")
COIN_PARAMS = {
    "BTC": {"hrp": "bc", "p2pkh": 0x00},
    "LTC": {"hrp": "ltc", "p2pkh": 0x30},
}


def b58decode_check(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes(82, "big").lstrip(b"\x00") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    raw = b"\x00" * pad + raw
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("Invalid xpub (checksum mismatch)")
    return payload


def b58encode_check(payload):
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def hash160(data):
    return RIPEMD160.new(hashlib.sha256(data).digest()).digest()


def _compress(point):
    return (b"\x02" if point.y() % 2 == 0 else b"\x03") + point.x().to_bytes(32, "big")


def ckd_pub(pubkey, chain, index):
    if index >= 0x80000000:
        raise ValueError("Hardened derivation is not possible from an xpub")
    digest = hmac.new(chain, pubkey + index.to_bytes(4, "big"), hashlib.sha512).digest()
    il = int.from_bytes(digest[:32], "big")
    if il >= SECP256k1.order:
        raise ValueError("Invalid child, try next index")
    try:
        parent = VerifyingKey.from_string(pubkey, curve=SECP256k1).pubkey.point
    except MalformedPointError as exc:
        raise ValueError("Invalid public key in wallet configuration.") from exc
    child = SECP256k1.generator * il + parent
    if child.x() is None:
        raise ValueError("Invalid derived public key.")
    return _compress(child), digest[32:]


def parse_xpub(xpub):
    xpub = xpub.strip()
    if len(xpub) != 111:
        raise ValueError("Expected an extended public wallet key.")
    payload = b58decode_check(xpub)
    if len(payload) != 78 or payload[45] not in (2, 3):
        raise ValueError("Expected an extended public wallet key.")
    return payload[:4], payload[13:45], payload[45:78]


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def segwit_address(hrp, program):
    bits, acc, data = 0, 0, []
    for byte in program:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    values = [0] + data
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _bech32_polymod(hrp_exp + values + [0] * 6) ^ 1
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(BECH32[d] for d in values + checksum)


def eth_address(compressed_pubkey):
    point = VerifyingKey.from_string(compressed_pubkey, curve=SECP256k1).pubkey.point
    raw = point.x().to_bytes(32, "big") + point.y().to_bytes(32, "big")
    addr = keccak.new(digest_bits=256, data=raw).digest()[-20:].hex()
    chk = keccak.new(digest_bits=256, data=addr.encode()).hexdigest()
    return "0x" + "".join(c.upper() if int(chk[i], 16) >= 8 else c for i, c in enumerate(addr))


def address_from_xpub(coin, xpub, index):
    version, chain, key = parse_xpub(xpub)
    if coin == "BTC" and version not in (XPUB_VERSION, ZPUB_VERSION):
        raise ValueError("Use the xpub or zpub from a standard Bitcoin mainnet wallet.")
    if coin == "LTC" and version not in (XPUB_VERSION, ZPUB_VERSION, LTUB_VERSION):
        raise ValueError("Unsupported Litecoin wallet key.")
    key, chain = ckd_pub(key, chain, 0)
    key, chain = ckd_pub(key, chain, index)
    if coin in ("ETH", "USDT"):
        return eth_address(key)
    params = COIN_PARAMS[coin]
    if version == ZPUB_VERSION:
        return segwit_address(params["hrp"], hash160(key))
    return b58encode_check(bytes([params["p2pkh"]]) + hash160(key))


def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def disabled_coins():
    return {c.upper() for c in _load("shop_config.json", {}).get("payments", {}).get("disabled_coins", [])}


def xpub_for_coin(coin):
    if coin.upper() in disabled_coins():
        return ""
    key = "ETH" if coin == "USDT" else coin
    raw_xpubs = os.environ.get("XPUBS_JSON")
    if raw_xpubs is None:
        xpubs = _load(XPUBS_FILE, {})
    else:
        try:
            xpubs = json.loads(raw_xpubs)
        except json.JSONDecodeError as exc:
            raise ValueError("XPUBS_JSON must contain a JSON object of public wallet keys") from exc
        if not isinstance(xpubs, dict) or any(not isinstance(value, str) for value in xpubs.values()):
            raise ValueError("XPUBS_JSON must contain a JSON object of public wallet keys")
    return (xpubs.get(key) or "").strip()


def configured_coins():
    # Telegram Crypto Bot is the only payment method when its API token is set.
    if os.environ.get("CRYPTO_PAY_TOKEN", "").strip():
        return ["CRYPTOBOT"]
    available = []
    for coin in ("BTC", "LTC", "ETH", "USDT"):
        try:
            xpub = xpub_for_coin(coin)
            if xpub:
                address_from_xpub(coin, xpub, 0)
                available.append(coin)
        except ValueError:
            # Hide invalid payment options without exposing wallet configuration.
            continue
    return available
