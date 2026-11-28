"""Website for adeleai.org: plans, crypto checkout through Shkeeper, order status, Shkeeper callback.

Runs behind nginx on 127.0.0.1:WEB_PORT. Only the standard library is used, like bot.py.
"""
import html
import json
import logging
import math
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
TAGLINE = "A members-only Telegram group with working prompts, workflows and updates for the current AI models."
INSIDE = [
    ("Private Telegram group", "You join with a single-use invitation as soon as your key is redeemed."),
    ("Prompts and workflows", "Tested material for the current top models, posted inside the group."),
    ("First to know", "New posts reach members before they appear anywhere else."),
]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {"/static/fonts/archivo-latin.woff2": "font/woff2",
                "/static/fonts/martian-mono-latin.woff2": "font/woff2"}

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


def _rosette(cx=100, cy=100):
    """Guilloche rosette from rotated ellipses and a ring of circles, as on security print."""
    ell = "".join(f'<ellipse cx="{cx}" cy="{cy}" rx="92" ry="34" transform="rotate({k * 7.5} {cx} {cy})"/>' for k in range(24))
    ring = "".join(f'<circle cx="{cx + 46 * math.cos(math.radians(k * 10)):.1f}" cy="{cy + 46 * math.sin(math.radians(k * 10)):.1f}" r="40"/>'
                   for k in range(36))
    return f'<g class="g1">{ell}</g><g class="g2">{ring}</g>'


def _waves(width=600, height=36, lines=7, step=6):
    paths = []
    for k in range(lines):
        pts = []
        for x in range(0, width + 1, step):
            y = height / 2 + (height / 2 - 3) * math.sin(2 * math.pi * x / 75 + k * 0.45) * math.cos(2 * math.pi * x / 600)
            pts.append(f"{x} {y:.1f}")
        paths.append(f'<path d="M{"L".join(pts)}"/>')
    return "".join(paths)


SEAL = f'<svg class="seal" viewBox="0 0 200 200" aria-hidden="true" focusable="false">{_rosette()}</svg>'
BAND = ('<svg class="band" viewBox="0 0 600 36" preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        f'{_waves()}</svg>')

CSS = """
@font-face{font-family:"Archivo";src:url(/static/fonts/archivo-latin.woff2) format("woff2");font-weight:100 900;font-stretch:62% 125%;font-display:swap}
@font-face{font-family:"Martian Mono";src:url(/static/fonts/martian-mono-latin.woff2) format("woff2");font-weight:100 800;font-stretch:75% 112.5%;font-display:swap}
:root{--ground:#17163a;--ground-2:#211f4d;--on-ground:#e3e5f2;--on-ground-2:#a9adcf;--paper:#e6ebee;--paper-2:#d9e0e5;
--ink:#1e1c45;--ink-2:#4a4c72;--guilloche:#5b63a8;--brand:#ffc93c;--brand-ink:#1e1c45;--stamp:#b4236e;--ok:#1f7a4d;
--sans:"Archivo",system-ui,sans-serif;--mono:"Martian Mono",ui-monospace,monospace;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scrollbar-color:#4a4c86 var(--ground)}
body{margin:0;background:var(--ground);color:var(--on-ground);font:400 1rem/1.6 var(--sans);caret-color:var(--brand);overflow-x:clip}
::selection{background:var(--brand);color:var(--brand-ink)}
a{color:var(--brand);text-underline-offset:.2em;text-decoration-thickness:1px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:3px solid var(--brand);outline-offset:3px;border-radius:4px}
.wrap{position:relative;max-width:46rem;margin:0 auto;padding:1.25rem 1rem 4rem}
header,main,footer{position:relative;z-index:1}
header{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;margin-bottom:2.25rem}
.mark{font-weight:800;font-stretch:125%;font-size:1.05rem;letter-spacing:-.01em;color:var(--on-ground);text-decoration:none}
.mark span{font-weight:400}
header a:not(.mark){font-size:.95rem}
.cert{position:relative;background:var(--paper);color:var(--ink);border-radius:6px;overflow:hidden;
box-shadow:0 1px 0 #ffffff80 inset,0 18px 40px -18px #05041a,0 2px 6px #05041a66}
.band{display:block;width:100%;height:2.25rem;fill:none;stroke:var(--guilloche);stroke-width:.8;opacity:.55;background:var(--paper-2)}
.cert-body{position:relative;padding:1.5rem 1.25rem 1.75rem}
.seal{position:absolute;top:.5rem;right:-1.25rem;width:8.5rem;height:8.5rem;fill:none;stroke-width:.45;opacity:.45}
.seal .g1{stroke:var(--guilloche)}.seal .g2{stroke:#a8841c}
h1,h2{font-weight:800;font-stretch:112%;letter-spacing:-.02em;line-height:1.08;text-wrap:balance;margin:0}
h1{position:relative;font-size:clamp(1.85rem,7.4vw,3rem);max-width:13ch;padding-right:2rem}
.lede{position:relative;margin:.9rem 0 0;max-width:34rem;color:var(--ink-2);font-size:1.05rem}
.serial{margin:1.6rem 0 0;display:flex;flex-wrap:wrap;align-items:baseline;gap:.25rem 1rem}
.serial-label{font-size:.85rem;color:var(--ink-2)}
.key{font:500 clamp(1.05rem,4.6vw,1.6rem)/1.2 var(--mono);font-stretch:100%;letter-spacing:.02em;color:var(--ink);white-space:nowrap}
.redact{display:inline-block;width:3.1em;height:.8em;margin:0 .05em;vertical-align:-.02em;background:var(--ink);border-radius:2px}
.foil{height:.55rem;margin:1.4rem -1.25rem 0;background:linear-gradient(100deg,#c7d6f6,#f5d0e8 18%,#fff1b8 34%,#c8f0e4 52%,#cfd2fb 70%,#f6d6c4 86%,#c7d6f6);background-size:200% 100%}
.buy{margin-top:1.5rem}
fieldset{border:0;margin:0 0 1.25rem;padding:0;min-width:0}
legend{padding:0;margin-bottom:.55rem;font-weight:700;font-size:.95rem}
.terms{display:grid;grid-template-columns:repeat(3,1fr)}
.stub{position:relative;display:flex;flex-direction:column;gap:.1rem;padding:.8rem .75rem;border:1px solid #b9c2cc;background:#eef2f4;cursor:pointer}
.stub+.stub{border-left:1px dashed #8d97a6}
.stub:first-of-type{border-radius:6px 0 0 6px}.stub:last-of-type{border-radius:0 6px 6px 0}
.stub input,.coin input{position:absolute;opacity:0;width:1px;height:1px}
.stub-days{font-size:.9rem;color:var(--ink-2)}
.stub-price{font:500 1.35rem/1.2 var(--mono)}
.stub:hover{background:#f6f8f9}
.stub:has(input:checked){background:var(--ink);color:var(--paper);border-color:var(--ink)}
.stub:has(input:checked) .stub-days{color:#c9cce6}
.stub:has(input:focus-visible),.coin:has(input:focus-visible){outline:3px solid var(--guilloche);outline-offset:2px}
.coins{display:grid;grid-template-columns:1fr 1fr;gap:.45rem}
.coin{position:relative;display:flex;align-items:center;gap:.6rem;padding:.6rem .7rem;border:1px solid #b9c2cc;border-radius:6px;background:#eef2f4;cursor:pointer;font-size:.92rem;line-height:1.3}
.coin::before{content:"";flex:none;width:.9rem;height:.9rem;border-radius:50%;border:1.5px solid var(--ink-2);background:#fff}
.coin:hover{background:#f6f8f9}
.coin:has(input:checked){border-color:var(--ink);background:#fff}
.coin:has(input:checked)::before{border-color:var(--ink);background:var(--ink);box-shadow:inset 0 0 0 2.5px #fff}
.cta{display:block;width:100%;padding:1rem 1.25rem;border:0;border-radius:6px;background:var(--brand);color:var(--brand-ink);
font:800 1.05rem/1 var(--sans);font-stretch:112%;letter-spacing:-.01em;cursor:pointer;text-align:center;text-decoration:none;
box-shadow:0 1px 0 #fff6 inset,0 6px 14px -8px #8a6a00}
.cta:hover{background:#ffd35f}
.cta:active{transform:translateY(1px)}
.fine{margin:.85rem 0 0;font-size:.85rem;line-height:1.5;color:var(--ink-2);max-width:36rem}
.notice{margin:0 0 1.1rem;padding:.75rem .9rem;border:1px solid var(--stamp);border-radius:6px;background:#f7e3ee;color:#6d1442;font-size:.95rem}
.order-meta{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:1.1rem}
.order-no{font:500 .8rem/1.3 var(--mono);color:var(--ink-2);word-break:break-all}
.stamp{flex:none;padding:.35rem .7rem;border:3px double currentColor;border-radius:6px;
font:800 .95rem/1 var(--sans);font-stretch:125%;transform:rotate(-6deg);mix-blend-mode:multiply}
.stamp-wait{color:var(--stamp)}.stamp-ok{color:var(--ok)}
.pay{margin:1.4rem 0 0}
.pay dt{font-size:.85rem;color:var(--ink-2);margin-top:1rem}
.pay dd{margin:.2rem 0 0}
.amount{font:500 clamp(1.3rem,5.6vw,2rem)/1.15 var(--mono);letter-spacing:-.01em}
.amount small{font:600 .95rem/1.2 var(--sans);letter-spacing:0;color:var(--ink-2);margin-left:.35rem}
.addr{display:block;padding:.75rem .85rem;border:1px solid #b9c2cc;border-radius:6px;background:#fff;
font:500 .92rem/1.5 var(--mono);word-break:break-all;user-select:all;-webkit-user-select:all}
.serial-big{display:block;margin-top:1.6rem}
.serial-big .serial-label{display:block;margin-bottom:.35rem}
.serial-big .key{font-size:clamp(1.45rem,7.4vw,2.5rem);letter-spacing:.01em}
.key-live{position:relative;display:inline-block}
.key-live::after{content:"";position:absolute;inset:-.1em -.15em;background:var(--ink);transform-origin:right center;
animation:unredact .9s cubic-bezier(.16,1,.3,1) .35s both}
@keyframes unredact{to{transform:scaleX(0)}}
@media (prefers-reduced-motion:reduce){.key-live::after{animation:none;transform:scaleX(0)}}
.after{margin-top:3.25rem}
.after h2{font-size:clamp(1.35rem,4.6vw,1.8rem);color:var(--on-ground);margin-bottom:1.1rem}
.steps{list-style:none;margin:0;padding:0;counter-reset:step;display:grid;gap:1rem}
.steps li{counter-increment:step;display:grid;grid-template-columns:2.25rem 1fr;gap:.75rem;align-items:baseline;color:var(--on-ground-2)}
.steps li::before{content:counter(step);font:500 1.1rem/1 var(--mono);color:var(--brand)}
.steps strong{color:var(--on-ground);font-weight:700}
.inside{margin:0;display:grid;gap:0}
.inside div{padding:.9rem 0;border-top:1px solid #34327a}
.inside dt{font-weight:700;color:var(--on-ground)}
.inside dd{margin:.15rem 0 0;color:var(--on-ground-2)}
footer{margin-top:3.5rem;padding-top:1.25rem;border-top:1px solid #34327a;color:var(--on-ground-2);font-size:.9rem;display:flex;flex-wrap:wrap;gap:.5rem 1.5rem}
@media (min-width:44rem){
.wrap{padding-top:2rem}
.cert-body{padding:2.25rem 2.5rem 2.5rem}
.seal{width:11rem;height:11rem;top:1rem;right:1.25rem}
.foil{margin-left:-2.5rem;margin-right:-2.5rem}
.coins{grid-template-columns:repeat(3,1fr)}
.steps{grid-template-columns:repeat(3,1fr);gap:1.5rem}
.steps li{grid-template-columns:1fr;gap:.4rem}
.stamp{font-size:1.15rem}
}
"""


def page(title, body, refresh=None):
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<meta name="theme-color" content="#17163a">
<link rel="preload" href="/static/fonts/archivo-latin.woff2" as="font" type="font/woff2" crossorigin>
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<header><a class="mark" href="/">AdeleAI <span>Private</span></a>
<a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a></header>
<main>{body}</main>
<footer><span>Payments are processed in crypto through our own payment server.</span>
<a href="https://t.me/{html.escape(TG_BOT)}">@{html.escape(TG_BOT)}</a></footer>
</div></body></html>""".encode("utf-8")


def _redacted_key():
    return ('<span class="key" aria-label="Your key appears here after payment">ADL-'
            '<span class="redact"></span>-<span class="redact"></span>-<span class="redact"></span></span>')


def landing(error=""):
    coins = shkeeper_api.available_coins()
    err = f'<p class="notice" role="alert">{html.escape(error)}</p>' if error else ""
    if coins:
        terms = "".join(
            f'<label class="stub"><input type="radio" name="plan" value="{p}"{" checked" if p == "90" else ""}>'
            f'<span class="stub-days">{days} days</span><span class="stub-price">€{html.escape(eur)}</span></label>'
            for p, (eur, days) in cfg.PLANS.items())
        options = "".join(
            f'<label class="coin"><input type="radio" name="coin" value="{c}"{" checked" if i == 0 else ""}>'
            f'{html.escape(cfg.COIN_LABELS[c])}</label>' for i, c in enumerate(coins))
        checkout = (f'<form class="buy" method="post" action="/order">{err}'
                    f'<fieldset class="terms"><legend>Term</legend>{terms}</fieldset>'
                    f'<fieldset class="coins"><legend>Pay with</legend>{options}</fieldset>'
                    '<button class="cta" type="submit">Continue to payment</button>'
                    '<p class="fine">The next page shows the exact amount and a payment address. '
                    'Your key is issued as soon as the payment is confirmed.</p></form>')
    else:
        checkout = (f'<div class="buy">{err}<p class="notice">Checkout on the website is paused right now. '
                    f'You can still buy in the <a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a>.</p></div>')
    discord = f' or on <a href="{html.escape(DISCORD_INVITE)}">Discord</a>' if DISCORD_INVITE else ""
    inside = "".join(f"<div><dt>{html.escape(t)}</dt><dd>{html.escape(d)}</dd></div>" for t, d in INSIDE)
    return page("AdeleAI Private", f"""
<article class="cert">{BAND}<div class="cert-body">{SEAL}
<h1>Your key to AdeleAI Private</h1>
<p class="lede">{html.escape(TAGLINE)}</p>
<p class="serial"><span class="serial-label">Access key</span>{_redacted_key()}</p>
<div class="foil" aria-hidden="true"></div>
{checkout}
</div>{BAND}</article>
<section class="after"><h2>How it works</h2><ol class="steps">
<li><span><strong>Pick a term and a coin.</strong> You get an address and the exact amount to send.</span></li>
<li><span><strong>Pay from any wallet.</strong> Your order page updates by itself and shows your key once the payment is confirmed.</span></li>
<li><span><strong>Open the key in Telegram.</strong> The bot checks it and sends your invitation to the private group.</span></li>
</ol></section>
<section class="after"><h2>Inside the group</h2><dl class="inside">{inside}</dl>
<p class="fine" style="color:var(--on-ground-2)">You can also buy directly in the <a href="https://t.me/{html.escape(TG_BOT)}">Telegram bot</a>{discord}.</p></section>""")


STATUS_TEXT = {
    "NEW": ("Creating invoice", "stamp-wait", "This order has no payment address"),
    "PENDING": ("Awaiting payment", "stamp-wait", "Waiting for your payment"),
    "PARTIAL": ("Partly paid", "stamp-wait", "Part of your payment arrived"),
    "PAID": ("Paid", "stamp-ok", "Your key is ready"),
    "REVIEW": ("Under review", "stamp-wait", "Your payment needs a check"),
    "FAILED": ("Invoice failed", "stamp-wait", "This order has no payment address"),
}


def _cert(order, heading, inner):
    label, css, _ = STATUS_TEXT.get(order["status"], (order["status"], "stamp-wait", ""))
    return (f'<article class="cert">{BAND}<div class="cert-body">'
            f'<div class="order-meta"><span class="order-no">Order {html.escape(order["order_id"])}</span>'
            f'<span class="stamp {css}">{html.escape(label)}</span></div>'
            f'<h1>{html.escape(heading)}</h1>'
            f'<p class="lede">{order["days"]} days of access for €{html.escape(order["eur"])}</p>'
            f'{inner}</div>{BAND}</article>')


def order_page(order):
    now = int(time.time())
    coin = html.escape(cfg.COIN_LABELS.get(order["crypto"], order["crypto"]))
    title = f"Order {order['order_id']}"
    heading = STATUS_TEXT.get(order["status"], ("", "", "Your order"))[2]
    bot = html.escape(TG_BOT)
    if order["status"] == "PAID":
        if order["key"]:
            key = html.escape(order["key"])
            inner = (f'<p class="serial serial-big"><span class="serial-label">Access key</span>'
                     f'<span class="key key-live">{key}</span></p><div class="foil" aria-hidden="true"></div>'
                     f'<div class="buy"><a class="cta" href="https://t.me/{bot}?start=key_{key}">Activate in Telegram</a>'
                     f'<p class="fine">The key works once. You can also paste it as a message into '
                     f'<a href="https://t.me/{bot}" style="color:var(--ink)">@{bot}</a>. '
                     'Keep this page private: anyone with the link can see the key.</p></div>')
        else:
            heading = "Access added to your Telegram account"
            inner = ('<div class="foil" aria-hidden="true"></div><p class="fine" style="font-size:1rem;color:var(--ink)">'
                     'The bot has sent your invitation to the private group.</p>')
        return page(title, _cert(order, heading, inner))
    if order["status"] in ("NEW", "FAILED") or not order["address"]:
        inner = ('<p class="notice" style="margin-top:1.4rem">'
                 '<a href="/" style="color:inherit">Start a new order</a> to get a payment address.</p>')
        return page(title, _cert(order, STATUS_TEXT["FAILED"][2], inner))
    stale = now > order["expires"] and order["status"] == "PENDING"
    notes = ""
    if stale:
        notes += ('<p class="notice" style="margin-top:1.4rem">This amount was calculated more than an hour ago. '
                  'If you have not paid yet, <a href="/" style="color:inherit">start a new order</a> for a current amount. '
                  'A payment you already sent is still credited.</p>')
    if order["status"] == "PARTIAL":
        notes += ('<p class="notice" style="margin-top:1.4rem">Send the missing amount to the same address. '
                  'Your key is issued when the full amount has arrived.</p>')
    if order["status"] == "REVIEW":
        notes += (f'<p class="notice" style="margin-top:1.4rem">Contact support in <a href="https://t.me/{bot}" '
                  'style="color:inherit">the Telegram bot</a> and include the order number.</p>')
    inner = (f'{notes}<dl class="pay"><dt>Send exactly</dt>'
             f'<dd class="amount">{html.escape(order["amount_crypto"])}<small>{coin}</small></dd>'
             f'<dt>To this address</dt><dd><span class="addr">{html.escape(order["address"])}</span></dd></dl>'
             f'<p class="fine">Send only {coin} on this network. Another coin or network cannot be recovered. '
             'Tap the address once to select it. This page refreshes by itself and shows your key when the payment is confirmed.</p>'
             f'<p class="serial"><span class="serial-label">Access key</span>{_redacted_key()}</p>'
             '<div class="foil" aria-hidden="true"></div>')
    return page(title, _cert(order, heading, inner), refresh=20)


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
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
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
        elif path in STATIC_TYPES:
            try:
                with open(os.path.join(STATIC_DIR, path[len("/static/"):]), "rb") as f:
                    data = f.read()
            except OSError:
                self.send_body(404, b"not found", "text/plain")
                return
            self.send_body(200, data, STATIC_TYPES[path], {"Cache-Control": "public, max-age=604800"})
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
        if not shkeeper_api.verify_any(cfg.SHKEEPER_API_KEYS, body, self.headers.get("X-Shkeeper-Timestamp", ""),
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
