"""Website for adeleai.org: plans, crypto checkout through Shkeeper, order status, Shkeeper callback.

Runs behind Caddy on 127.0.0.1:WEB_PORT. Only the standard library is used, like bot.py.
"""
import html
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import access
import payment_config as cfg
import shkeeper_api

DB = os.environ.get("DATABASE_PATH", "adeleai.sqlite3")
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8088"))
TG_BOT = os.environ.get("TELEGRAM_BOT_USERNAME", "AdlesesAI_bot")
DISCORD_INVITE = os.environ.get("DISCORD_INVITE_URL", "")
ORDERS_PER_IP_PER_HOUR = int(os.environ.get("ORDERS_PER_IP_PER_HOUR", "10"))
MAX_BODY = 256 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

# Landing page copy. Edit freely; it is plain text and gets HTML-escaped.
TAGLINE = "Private members' group for working AI prompts, workflows and updates."
FEATURES = [
    ("🔒", "Private Telegram group", "Access through a single-use invitation after payment."),
    ("⚡", "Prompts and workflows", "Tested material for the current top models."),
    ("📢", "First to know", "New posts reach members before anyone else."),
]

_rate_lock = threading.Lock()
_rate = {}


def allow_order(ip, now=None):
    now = time.time() if now is None else now
    with _rate_lock:
        recent = [t for t in _rate.get(ip, []) if now - t < 3600]
        if len(recent) >= ORDERS_PER_IP_PER_HOUR:
            _rate[ip] = recent
            return False
        recent.append(now)
        _rate[ip] = recent
        if len(_rate) > 10000:
            for key in [k for k, v in _rate.items() if not v or now - v[-1] >= 3600]:
                del _rate[key]
        return True


CSS = """
:root{--bg:#0b0d12;--card:#151923;--line:#252b38;--text:#e8ebf2;--muted:#9aa3b5;--accent:#ffc93c;--ok:#3ecf8e;--warn:#f5a524}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--accent)}main{max-width:880px;margin:0 auto;padding:32px 16px 64px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:40px}
.brand{font-weight:800;font-size:20px;text-decoration:none;color:var(--text)}.brand span{color:var(--accent)}
h1{font-size:clamp(28px,6vw,44px);line-height:1.15;margin:0 0 12px}h2{margin:40px 0 16px}
.muted{color:var(--muted)}.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.plan{cursor:pointer;display:block}.plan input{margin-right:8px}.price{font-size:28px;font-weight:800}
.plan:has(input:checked){border-color:var(--accent)}
select,button{font:inherit;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0f1219;color:var(--text);width:100%}
button{background:var(--accent);color:#111;font-weight:700;border:0;cursor:pointer;margin-top:12px}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.addr{background:#0f1219;border:1px solid var(--line);border-radius:10px;padding:12px;margin:8px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:14px;font-weight:600}
.b-ok{background:#123326;color:var(--ok)}.b-warn{background:#3a2a0d;color:var(--warn)}.b-muted{background:#1d2230;color:var(--muted)}
footer{margin-top:56px;color:var(--muted);font-size:14px;display:flex;gap:16px;flex-wrap:wrap}
.notice{border-left:3px solid var(--warn);padding:8px 12px;background:#1b1710;border-radius:6px}
"""


def page(title, body, refresh=None):
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<header><a class="brand" href="/">Adele<span>AI</span> Private</a>
<a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a></header>
{body}
<footer><span>Payments are processed in crypto through our own payment server.</span></footer>
</main></body></html>""".encode("utf-8")


def landing(error=""):
    coins = shkeeper_api.available_coins()
    features = "".join(f'<div class="card"><div style="font-size:28px">{i}</div><h3>{html.escape(t)}</h3>'
                       f'<p class="muted">{html.escape(d)}</p></div>' for i, t, d in FEATURES)
    plans = "".join(
        f'<label class="card plan"><input type="radio" name="plan" value="{p}"{" checked" if p == "90" else ""}>'
        f'<strong>{days} days</strong><div class="price">€{eur}</div></label>'
        for p, (eur, days) in cfg.PLANS.items())
    if coins:
        options = "".join(f'<option value="{c}">{html.escape(cfg.COIN_LABELS[c])}</option>' for c in coins)
        checkout = (f'<form method="post" action="/order"><div class="grid">{plans}</div>'
                    f'<h3>Pay with</h3><select name="coin" required>{options}</select>'
                    f'<button type="submit">Continue to payment</button></form>')
    else:
        checkout = (f'<p class="notice">Website checkout is temporarily unavailable. You can still buy in the '
                    f'<a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a>.</p>')
    err = f'<p class="notice">{html.escape(error)}</p>' if error else ""
    discord = f' or on <a href="{html.escape(DISCORD_INVITE)}">Discord</a>' if DISCORD_INVITE else ""
    return page("AdeleAI Private", f"""
<h1>AdeleAI Private</h1><p class="muted">{html.escape(TAGLINE)}</p>
<div class="grid">{features}</div>
<h2>Choose a plan</h2>{err}{checkout}
<h2>How it works</h2><ol class="muted">
<li>Pick a plan and a coin. You get an address and the exact amount.</li>
<li>After the payment is confirmed, your order page shows a single-use access key.</li>
<li>Tap "Activate in Telegram". The bot checks the key and sends your private invitation.</li></ol>
<p class="muted">You can also buy directly in the <a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a>{discord}.</p>""")


STATUS_TEXT = {
    "NEW": ("Creating invoice", "b-muted"), "PENDING": ("Waiting for payment", "b-warn"),
    "PARTIAL": ("Partially paid", "b-warn"), "PAID": ("Paid", "b-ok"),
    "REVIEW": ("Under review, contact support", "b-warn"), "FAILED": ("Invoice failed", "b-muted"),
}


def order_page(order):
    label, css = STATUS_TEXT.get(order["status"], (order["status"], "b-muted"))
    now = int(time.time())
    coin = html.escape(cfg.COIN_LABELS.get(order["crypto"], order["crypto"]))
    head = (f'<h1>Order {html.escape(order["order_id"])}</h1><p><span class="badge {css}">{label}</span> '
            f'<span class="muted">{order["days"]} days · €{html.escape(order["eur"])}</span></p>')
    if order["status"] == "PAID":
        if order["key"]:
            key = html.escape(order["key"])
            body = (f'<div class="card"><h3>Your access key</h3><div class="addr mono">{key}</div>'
                    f'<a href="https://t.me/{html.escape(TG_BOT)}?start=key_{key}"><button>Activate in Telegram</button></a>'
                    f'<p class="muted">The key works once. You can also paste it as a message into '
                    f'<a href="https://t.me/{html.escape(TG_BOT)}">@{html.escape(TG_BOT)}</a>. Keep this page private.</p></div>')
        else:
            body = '<div class="card"><p>Access was added to your Telegram account. The bot has sent your invitation.</p></div>'
        return page(f"Order {order['order_id']}", head + body)
    if order["status"] in ("NEW", "FAILED") or not order["address"]:
        body = '<p class="notice">No payment address for this order. Please <a href="/">start a new order</a>.</p>'
        return page(f"Order {order['order_id']}", head + body)
    stale = now > order["expires"] and order["status"] == "PENDING"
    warn = ('<p class="notice">This quote is older than one hour. If you have not paid yet, '
            '<a href="/">start a new order</a> for a current amount. A payment already sent is still credited.</p>'
            if stale else "")
    body = (f'{warn}<div class="card"><p>Send exactly</p><div class="addr mono">{html.escape(order["amount_crypto"])} '
            f'· {coin}</div><p>to this address</p><div class="addr mono">{html.escape(order["address"])}</div>'
            f'<p class="muted">Only send {coin} on this network. Sending another coin or network loses the funds. '
            f'This page refreshes automatically and shows your access key once the payment is confirmed. '
            f'Bookmark it.</p></div>')
    return page(f"Order {order['order_id']}", head + body, refresh=20)


class Handler(BaseHTTPRequestHandler):
    server_version = "AdeleAI"
    sys_version = ""

    def client_ip(self):
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For", "")
        if peer in ("127.0.0.1", "::1") and forwarded:
            return forwarded.split(",")[-1].strip()
        return peer

    def send_body(self, status, body, content_type="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_body(200, landing())
        elif path == "/healthz":
            self.send_body(200, b"ok", "text/plain")
        elif path.startswith("/order/"):
            db = access.connect(DB)
            try:
                order = access.order_by_token(db, path[len("/order/"):])
            finally:
                db.close()
            if order is None:
                self.send_body(404, page("Not found", "<h1>Order not found</h1>"))
            else:
                self.send_body(200, order_page(order), extra={"Cache-Control": "no-store"})
        else:
            self.send_body(404, page("Not found", "<h1>Not found</h1>"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self.read_body()
        if body is None:
            self.send_body(413, b"payload too large", "text/plain")
            return
        if path == cfg.CALLBACK_PATH:
            self.shkeeper_callback(body)
        elif path == "/order":
            self.new_order(body)
        else:
            self.send_body(404, b"not found", "text/plain")

    def new_order(self, body):
        form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
        plan, coin = form.get("plan", [""])[0], form.get("coin", [""])[0]
        if plan not in cfg.PLANS or coin not in shkeeper_api.available_coins():
            self.send_body(400, landing("Please choose a plan and an available coin."))
            return
        if not allow_order(self.client_ip()):
            self.send_body(429, landing("Too many orders from your connection. Please try again later."))
            return
        db = access.connect(DB)
        try:
            order = access.create_order(db, "web", plan, coin)
        except Exception:
            log.exception("Order creation failed")
            self.send_body(502, landing("The payment service is not reachable right now. Please try again shortly."))
            return
        finally:
            db.close()
        self.send_body(303, b"", "text/plain", {"Location": f"/order/{order['token']}"})

    def shkeeper_callback(self, body):
        if not shkeeper_api.verify_signature(cfg.SHKEEPER_API_KEY, body, self.headers.get("X-Shkeeper-Timestamp", ""),
                                             self.headers.get("X-Shkeeper-Signature", "")):
            log.warning("Rejected Shkeeper callback with bad signature")
            self.send_body(401, b"bad signature", "text/plain")
            return
        try:
            payload = json.loads(body)
        except ValueError:
            # Signed but unparseable: acknowledge so Shkeeper stops resending; a human has to look.
            log.error("Signed Shkeeper callback is not JSON: %r", body[:500])
            self.send_body(202, b"accepted", "text/plain")
            return
        db = access.connect(DB)
        try:
            outcome = access.apply_callback(db, payload)
        except Exception:
            log.exception("Callback processing failed; Shkeeper will retry")
            self.send_body(500, b"retry", "text/plain")
            return
        finally:
            db.close()
        log.info("Shkeeper callback %s: %s", payload.get("external_id") if isinstance(payload, dict) else "?", outcome)
        self.send_body(202, b"accepted", "text/plain")

    def log_message(self, fmt, *args):
        # Anonymous shop: no per-request access log. Client IPs and order tokens (bearer secrets) are never written.
        return


def main():
    db = access.connect(DB)
    access.init_db(db)
    db.close()
    if not cfg.shkeeper_enabled():
        log.warning("SHKEEPER_URL or SHKEEPER_API_KEY is not set: checkout is disabled, callbacks will be rejected")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log.info("listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
