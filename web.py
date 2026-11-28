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
from decimal import Decimal
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

# Landing page copy. Edit freely; it is plain text and gets HTML-escaped. [TODO] marks content only you can supply.
TAGLINE = "A members-only Telegram group with working prompts and workflows for the current AI models."
INSIDE = [
    ("lock", "Private Telegram group", "You join with a single-use invitation as soon as your key is redeemed."),
    ("spark", "Prompts and workflows", "Tested material for the current top models, posted inside the group."),
    ("bell", "First to know", "New posts reach members before they appear anywhere else."),
]
INCLUDED = [
    "Access to the private Telegram group for the term you pick",
    "Everything posted in the group while your term runs",
    "Support through the Telegram bot",
]
NOT_INCLUDED = [
    "Automatic renewal: every term is a separate payment",
    "[TODO: weitere Leistungen, die nicht enthalten sind]",
]
FAQ = [
    ("What exactly do I get?",
     "Access to the private AdeleAI Telegram group for the term you choose. After payment you get a single-use key, "
     "and the Telegram bot turns it into your invitation. [TODO: Inhalte der Gruppe genauer beschreiben]"),
    ("How long is the key valid?",
     "Your term starts when you redeem the key in the Telegram bot, not when you pay. When the term ends, the bot "
     "removes you from the group; buy a new term to stay. [TODO: Gültigkeit noch nicht eingelöster Keys festlegen]"),
    ("What if my payment goes wrong or I send the wrong amount?",
     "If less than the shown amount arrives, your order page says so: send the missing amount to the same address. "
     "A payment that arrives after the one-hour price window is still credited. Coins sent on a different network "
     "than the one shown cannot be recovered. [TODO: Regel für Überzahlungen und Erstattungen]"),
    ("How do I reach support?",
     "In the Telegram bot, tap 🆘 Support and write your message. Include your order number if you have one. "
     "[TODO: Support-Kontakt]"),
]
FOOTER_LINKS = [("Imprint", "#"), ("Terms", "#"), ("Privacy", "#")]  # [TODO: Links zu den Seiten eintragen]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {"/static/fonts/archivo-latin.woff2": "font/woff2",
                "/static/fonts/martian-mono-latin.woff2": "font/woff2",
                "/static/fonts/unbounded-latin.woff2": "font/woff2",
                "/static/img/logo-96.webp": "image/webp",
                "/static/img/logo-256.webp": "image/webp",
                "/static/img/banner.webp": "image/webp"}

# Shkeeper coin -> (badge text, brand colour, name, network shown to the buyer or None)
COIN_META = {
    "BTC": ("BTC", "#f7931a", "Bitcoin", None),
    "LTC": ("LTC", "#345d9d", "Litecoin", None),
    "ETH": ("ETH", "#627eea", "Ethereum", None),
    "SOL": ("SOL", "#9945ff", "Solana", None),
    "SOLANA-USDT": ("USDT", "#26a17b", "Tether USDT", "Solana network"),
    "ETH-USDT": ("USDT", "#26a17b", "Tether USDT", "Ethereum network (ERC20)"),
    "USDT": ("USDT", "#26a17b", "Tether USDT", "Tron network (TRC20)"),
}

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


def per_month(eur, days):
    """Price per month for a plan: 30 days = 1 month, 90 = 3, 365 = 12; other lengths use 30-day months."""
    months = {30: 1, 90: 3, 365: 12}.get(days) or max(1, round(days / 30))
    return (Decimal(eur) / months).quantize(Decimal("0.01"))


def _rosette(cx=100, cy=100):
    """Guilloche rosette from rotated ellipses and a ring of circles, as on security print."""
    ell = "".join(f'<ellipse cx="{cx}" cy="{cy}" rx="92" ry="34" transform="rotate({k * 7.5} {cx} {cy})"/>' for k in range(24))
    ring = "".join(f'<circle cx="{cx + 46 * math.cos(math.radians(k * 10)):.1f}" cy="{cy + 46 * math.sin(math.radians(k * 10)):.1f}" r="40"/>'
                   for k in range(36))
    return f'<g>{ell}</g><g class="r2">{ring}</g>'


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

_ICON_PATHS = {
    "term": '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3 10h18M7 15h5"/>',
    "wallet": '<path d="M4 7.5h14.5A1.5 1.5 0 0 1 20 9v9a1.5 1.5 0 0 1-1.5 1.5H5.5A1.5 1.5 0 0 1 4 18z"/><path d="M4 7.5 15 4.5v3"/><path d="M16 13.5h.01"/>',
    "send": '<path d="M21 4 3 11.2l6.3 2.1L11.5 20l3.2-4.3L19 19z"/><path d="m9.3 13.3 8.2-6.3"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/><path d="M12 15v2"/>',
    "spark": '<path d="M12 3c.6 4.2 2.8 6.4 7 7-4.2.6-6.4 2.8-7 7-.6-4.2-2.8-6.4-7-7 4.2-.6 6.4-2.8 7-7z"/>',
    "bell": '<path d="M6 16v-5a6 6 0 1 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
}


def icon(name, cls="icon"):
    return (f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{_ICON_PATHS[name]}</svg>')


def coin_badge(coin):
    text, color, _, _ = COIN_META.get(coin, (coin[:4], "#6b6470", coin, None))
    size = "7" if len(text) > 3 else "8.5"
    return (f'<svg class="coin-badge" viewBox="0 0 32 32" aria-hidden="true" focusable="false">'
            f'<circle cx="16" cy="16" r="15" fill="{color}"/><text x="16" y="19.2" text-anchor="middle" '
            f'font-family="Archivo,sans-serif" font-weight="800" font-size="{size}" fill="#fff">{html.escape(text)}</text></svg>')


CSS = """
@font-face{font-family:"Unbounded";src:url(/static/fonts/unbounded-latin.woff2) format("woff2");font-weight:200 900;font-display:swap}
@font-face{font-family:"Archivo";src:url(/static/fonts/archivo-latin.woff2) format("woff2");font-weight:100 900;font-stretch:62% 125%;font-display:swap}
@font-face{font-family:"Martian Mono";src:url(/static/fonts/martian-mono-latin.woff2) format("woff2");font-weight:100 800;font-stretch:75% 112.5%;font-display:swap}
:root{--bg:#08070b;--bg-2:#100e14;--surface:#16131b;--surface-2:#1d1a23;--line:#2e2934;--line-2:#433b47;
--text:#f4efe6;--text-2:#bcb3a4;--gold:#f2c35b;--gold-hi:#ffe6a3;--gold-lo:#b98320;
--gold-grad:linear-gradient(135deg,#8a5a12 0%,#d9a23a 28%,#ffe29a 50%,#d39a2e 72%,#8a5a12 100%);
--ok:#4fd6a0;--warn:#ff9c87;--display:"Unbounded","Archivo",system-ui,sans-serif;--sans:"Archivo",system-ui,sans-serif;
--mono:"Martian Mono",ui-monospace,monospace;--radius:14px;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scrollbar-color:#4a3f2c var(--bg);scroll-behavior:smooth;overflow-x:clip}
body{margin:0;background:var(--bg);color:var(--text);font:400 1rem/1.6 var(--sans);caret-color:var(--gold);overflow-x:clip}
::selection{background:var(--gold);color:#1a1206}
a{color:var(--gold);text-underline-offset:.2em;text-decoration-thickness:1px}
a:hover{color:var(--gold-hi);text-decoration-thickness:2px}
:focus-visible{outline:3px solid var(--gold-hi);outline-offset:3px;border-radius:6px}
.wrap{position:relative;max-width:68rem;margin:0 auto;padding:0 1rem}
.skip{position:absolute;left:1rem;top:-3rem;background:var(--gold);color:#1a1206;padding:.5rem .8rem;border-radius:8px;z-index:10}
.skip:focus{top:.75rem}
header.site{display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:1rem 0}
.brand{display:flex;align-items:center;gap:.6rem;color:var(--text);text-decoration:none;font:600 1rem/1 var(--display);letter-spacing:-.01em}
.brand img{width:2.5rem;height:2.5rem;border-radius:10px;mix-blend-mode:screen}
.brand span{color:var(--text-2);font-weight:400}
header.site nav a{font-size:.95rem}
h1,h2,h3{font-family:var(--display);font-weight:700;letter-spacing:-.025em;line-height:1.08;text-wrap:balance;margin:0}
h1{font-size:clamp(2rem,7.6vw,3.5rem)}
h2{font-size:clamp(1.45rem,4.8vw,2.1rem);font-weight:600}
h3{font-size:1.05rem;font-weight:600;letter-spacing:-.01em;line-height:1.25}
.lede{margin:1rem 0 0;max-width:34rem;color:var(--text-2);font-size:1.1rem}
.hero{position:relative;padding:1.5rem 0 2.5rem}
.hero-bg{position:absolute;top:-5rem;bottom:0;left:calc(50% - 50vw);right:calc(50% - 50vw);z-index:-1;background:url(/static/img/banner.webp) 70% 30%/cover no-repeat;opacity:.2;
filter:saturate(.8);-webkit-mask-image:linear-gradient(180deg,#000 0%,#000 35%,transparent 92%);mask-image:linear-gradient(180deg,#000 0%,#000 35%,transparent 92%)}
.hero-bg::after{content:"";position:absolute;inset:0;background:radial-gradient(70% 60% at 70% 30%,transparent,var(--bg) 85%)}
.hero-grid{display:grid;gap:2rem;align-items:center}
.from{margin:1.25rem 0 0;color:var(--text);font-weight:600}
.from b{font:500 1.05rem/1 var(--mono);color:var(--gold)}
.ticket{position:relative;isolation:isolate;padding:1.5rem 1.4rem 1.25rem;border-radius:var(--radius);color:var(--text);
background:linear-gradient(160deg,#1b1710,#0d0b10 55%,#15110a) padding-box,var(--gold-grad) border-box;border:1.5px solid transparent;
box-shadow:0 24px 60px -24px #000,0 0 60px -20px #f2c35b55;overflow:hidden}
.ticket .seal{position:absolute;z-index:-1;right:-3.5rem;top:-3rem;width:15rem;height:15rem;fill:none;stroke:#e3ad3e;stroke-width:.4;opacity:.28}
.ticket .seal .r2{stroke:#fff1c7;opacity:.6}
.ticket .band{display:block;width:calc(100% + 2.8rem);height:1.6rem;margin:1.1rem -1.4rem 0;fill:none;stroke:#e3ad3e;stroke-width:.7;opacity:.45}
.t-head{display:flex;align-items:center;gap:.65rem}
.t-head img{width:2.6rem;height:2.6rem;border-radius:10px;mix-blend-mode:screen}
.t-name{font:600 1.05rem/1.1 var(--display)}
.t-sub{color:var(--text-2);font-size:.85rem}
.t-label{margin:1.6rem 0 .4rem;color:var(--text-2);font-size:.85rem}
.key{font:500 clamp(1.15rem,5.4vw,1.7rem)/1.2 var(--mono);letter-spacing:.02em;white-space:nowrap;color:var(--gold-hi)}
.redact{display:inline-block;width:3.05em;height:.78em;margin:0 .06em;vertical-align:-.02em;border-radius:3px;
background:linear-gradient(100deg,#3b3020 25%,#b48a36 45%,#ffe29a 50%,#b48a36 55%,#3b3020 75%);background-size:400% 100%;background-position:100% 0;
animation:shimmer 3.2s cubic-bezier(.45,0,.2,1) infinite}
.redact:nth-child(2){animation-delay:.12s}.redact:nth-child(3){animation-delay:.24s}
@keyframes shimmer{0%{background-position:100% 0}60%,100%{background-position:0 0}}
.foil{height:.4rem;margin:1.2rem -1.4rem 0;background:var(--gold-grad);background-size:200% 100%;opacity:.9}
.t-foot{margin:.9rem 0 0;color:var(--text-2);font-size:.85rem}
@media (hover:hover) and (prefers-reduced-motion:no-preference){
.ticket{transition:transform .6s cubic-bezier(.16,1,.3,1),box-shadow .6s;transform:perspective(1100px) rotateX(0) rotateY(0)}
.ticket:hover{transform:perspective(1100px) rotateX(4deg) rotateY(-6deg) translateY(-3px);box-shadow:0 34px 70px -26px #000,0 0 80px -18px #f2c35b77}}
section.block{padding:3rem 0 0}
.block>h2{margin-bottom:1.25rem}
.checkout{margin-top:.25rem;padding:1.25rem 1rem 0;border:1px solid var(--line);border-radius:var(--radius);background:var(--bg-2)}
.notice{margin:0 0 1rem;padding:.8rem 1rem;border:1px solid var(--warn);border-radius:10px;background:#2a1513;color:#ffd6cc}
.notice a{color:inherit}
fieldset{border:0;margin:0 0 1.5rem;padding:0;min-width:0}
legend{padding:0;margin-bottom:.7rem;font:600 1rem/1.2 var(--display)}
.terms{display:grid;gap:.6rem}
.term,.coin{position:relative;display:grid;cursor:pointer;border:1.5px solid var(--line);border-radius:12px;background:var(--surface);transition:border-color .2s,background .2s}
.term input,.coin input{position:absolute;opacity:0;width:1px;height:1px}
.term{grid-template-columns:1fr auto;align-items:center;gap:.2rem 1rem;padding:1rem 1.1rem}
.term-days{font:600 1.05rem/1.2 var(--display)}
.term-price{grid-row:span 2;font:500 1.75rem/1 var(--mono);color:var(--text);text-align:right}
.term-month{color:var(--text-2);font-size:.9rem}
.term .tick{position:absolute;top:.6rem;right:.6rem;width:1.1rem;height:1.1rem;color:#1a1206;background:var(--gold);border-radius:50%;padding:2px;opacity:0;transition:opacity .2s}
.term:hover,.coin:hover{border-color:var(--line-2);background:var(--surface-2)}
.term:has(input:checked),.coin:has(input:checked){border-color:var(--gold);background:linear-gradient(160deg,#2a2112,var(--surface) 70%)}
.term:has(input:checked) .term-price{color:var(--gold-hi)}
.term:has(input:checked) .tick{opacity:1}
.term:has(input:focus-visible),.coin:has(input:focus-visible){outline:3px solid var(--gold-hi);outline-offset:2px}
.coins{display:grid;grid-template-columns:1fr 1fr;gap:.55rem}
.coin{grid-template-columns:auto 1fr;align-items:center;gap:.15rem .7rem;padding:.75rem .8rem}
.coin-badge{grid-row:span 2;width:2rem;height:2rem}
.coin-name{font-weight:700;line-height:1.2}
.coin-net{color:var(--text-2);font-size:.82rem;line-height:1.25}
.coin-net.is-net{color:var(--gold)}
.coin.has-net{grid-column:1/-1}
.net-note{grid-column:1/-1;display:flex;gap:.5rem;align-items:flex-start;margin:.3rem 0 0;color:var(--text-2);font-size:.88rem;line-height:1.45}
.net-note .icon{flex:none;width:1.1rem;height:1.1rem;margin-top:.1rem;color:var(--gold)}
.checkout-bar{position:sticky;bottom:0;z-index:5;margin:0 -1rem;padding:.85rem 1rem calc(.85rem + env(safe-area-inset-bottom));
background:#0c0b10f2;border-top:1px solid var(--line);border-radius:0 0 var(--radius) var(--radius)}
.summary{display:flex;justify-content:space-between;align-items:baseline;gap:.75rem;margin:0 0 .65rem;font-size:.95rem;color:var(--text-2)}
.summary strong{color:var(--text);font-weight:700}
.sum-plan,.sum-coin,.sum-price{display:none}
.sum-price{font:500 1.15rem/1 var(--mono);color:var(--gold-hi)}
@supports selector(:has(*)){.sum-fallback{display:none}}
.cta{display:block;width:100%;padding:1.05rem 1.25rem;border:0;border-radius:12px;color:#1a1206;cursor:pointer;text-align:center;text-decoration:none;
font:700 1.02rem/1 var(--display);letter-spacing:-.01em;background:linear-gradient(180deg,#ffe6a3,#f0bd4e 45%,#c8922a);
box-shadow:0 1px 0 #fff8 inset,0 10px 28px -12px #f2c35b99}
.cta:hover{color:#1a1206;filter:brightness(1.06)}
.cta:active{transform:translateY(1px)}
.fine{margin:.9rem 0 1.25rem;font-size:.86rem;line-height:1.5;color:var(--text-2);max-width:40rem}
.timeline{list-style:none;margin:0;padding:0;display:grid;gap:0;counter-reset:step}
.timeline li{position:relative;display:grid;grid-template-columns:3rem 1fr;gap:1rem;padding-bottom:1.6rem;counter-increment:step}
.timeline li:last-child{padding-bottom:0}
.timeline li::after{content:"";position:absolute;left:1.5rem;top:3.1rem;bottom:.3rem;width:1px;background:linear-gradient(var(--gold-lo),transparent)}
.timeline li:last-child::after{display:none}
.step-icon{display:grid;place-items:center;width:3rem;height:3rem;border-radius:50%;border:1.5px solid var(--gold-lo);background:var(--surface);color:var(--gold)}
.step-icon .icon{width:1.35rem;height:1.35rem}
.step-n{display:block;margin-bottom:.15rem;font:500 .8rem/1.2 var(--mono);color:var(--gold)}
.timeline p{margin:.3rem 0 0;color:var(--text-2)}
.features{display:grid;gap:.75rem}
.feature{padding:1.2rem 1.2rem 1.1rem;border:1px solid var(--line);border-radius:var(--radius);background:var(--bg-2)}
.feature .icon{width:1.6rem;height:1.6rem;color:var(--gold);margin-bottom:.8rem}
.feature p{margin:.35rem 0 0;color:var(--text-2)}
.scope{display:grid;gap:.75rem;margin-top:.75rem}
.scope>div{padding:1.1rem 1.2rem;border:1px solid var(--line);border-radius:var(--radius)}
.scope ul{list-style:none;margin:.6rem 0 0;padding:0;display:grid;gap:.45rem}
.scope li{display:grid;grid-template-columns:1.25rem 1fr;gap:.55rem;color:var(--text-2)}
.scope li .icon{width:1.2rem;height:1.2rem;margin-top:.15rem}
.scope .yes .icon{color:var(--ok)}.scope .no .icon{color:var(--text-2)}
.faq details{border-bottom:1px solid var(--line)}
.faq details:first-of-type{border-top:1px solid var(--line)}
.faq summary{list-style:none;display:flex;justify-content:space-between;gap:1rem;padding:1.05rem 0;cursor:pointer;font-weight:700}
.faq summary::-webkit-details-marker{display:none}
.faq summary .icon{flex:none;width:1.2rem;height:1.2rem;color:var(--gold);transition:transform .25s}
.faq details[open] summary .icon{transform:rotate(45deg)}
.faq details p{margin:0 0 1.1rem;color:var(--text-2);max-width:44rem}
footer.site{margin-top:4rem;padding:1.5rem 0 calc(2rem + env(safe-area-inset-bottom));border-top:1px solid var(--line);color:var(--text-2);font-size:.9rem;
display:flex;flex-wrap:wrap;justify-content:space-between;gap:.75rem 2rem}
footer.site nav{display:flex;flex-wrap:wrap;gap:.5rem 1.25rem}
.order{max-width:40rem;margin:1rem auto 0}
.order .ticket{margin-top:1.4rem;padding-bottom:1.4rem}
.o-meta{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin-bottom:1rem}
.o-no{font:500 .78rem/1.3 var(--mono);color:var(--text-2);word-break:break-all}
.stamp{flex:none;padding:.35rem .7rem;border:3px double currentColor;border-radius:8px;font:700 .85rem/1 var(--display);transform:rotate(-5deg)}
.stamp-wait{color:var(--gold)}.stamp-ok{color:var(--ok)}.stamp-bad{color:var(--warn)}
.order h1{font-size:clamp(1.7rem,6.4vw,2.6rem)}
.order .lede{font-size:1rem;margin-top:.6rem}
.pay{margin:1.4rem 0 0}
.pay dt{font-size:.85rem;color:var(--text-2);margin-top:1rem}
.pay dd{margin:.25rem 0 0}
.amount{font:500 clamp(1.35rem,6vw,2rem)/1.15 var(--mono);color:var(--gold-hi)}
.amount small{font:600 .95rem/1.2 var(--sans);color:var(--text-2);margin-left:.4rem}
.addr{display:block;padding:.8rem .9rem;border:1px solid var(--line-2);border-radius:10px;background:#0b0a0e;color:var(--text);
font:500 .92rem/1.5 var(--mono);word-break:break-all;user-select:all;-webkit-user-select:all}
.key-big{font-size:clamp(1.45rem,7.2vw,2.4rem)}
.key-live{position:relative;display:inline-block}
.key-live::after{content:"";position:absolute;inset:-.1em -.15em;background:var(--gold-grad);transform-origin:right center;animation:unredact .9s cubic-bezier(.16,1,.3,1) .35s both}
@keyframes unredact{to{transform:scaleX(0)}}
.order .cta{margin-top:1.3rem}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}.redact{animation:none;background-position:50% 0}
.key-live::after{animation:none;transform:scaleX(0)}*{transition:none!important}}
@media (min-width:46rem){
.wrap{padding:0 1.5rem}
.hero{padding:3rem 0 3.5rem}
.hero-grid{grid-template-columns:1.1fr .9fr;gap:3rem}
.checkout{padding:1.75rem 1.75rem 0}
.checkout-bar{margin:0 -1.75rem;padding:1rem 1.75rem}
.terms{grid-template-columns:repeat(3,1fr)}
.term{grid-template-columns:1fr;padding:1.2rem 1.2rem 1.1rem;gap:.35rem}
.term-price{grid-row:auto;text-align:left;font-size:2rem;margin-top:.35rem}
.coins{grid-template-columns:repeat(3,1fr)}
.coin.has-net{grid-column:auto}
.timeline{grid-template-columns:repeat(3,1fr);gap:1.5rem}
.timeline li{grid-template-columns:1fr;gap:.9rem;padding:0}
.timeline li::after{left:3.6rem;right:-1.5rem;top:1.5rem;bottom:auto;width:auto;height:1px;background:linear-gradient(90deg,var(--gold-lo),transparent)}
.features{grid-template-columns:repeat(3,1fr)}
.scope{grid-template-columns:1fr 1fr}
.ticket{padding:1.75rem 1.75rem 1.4rem}
.ticket .band{width:calc(100% + 3.5rem);margin-left:-1.75rem;margin-right:-1.75rem}
.foil{margin-left:-1.75rem;margin-right:-1.75rem}
}
"""


def page(title, body, refresh=None, extra_css=""):
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    bot = html.escape(TG_BOT)
    links = "".join(f'<a href="{html.escape(href)}">{html.escape(label)}</a>' for label, href in FOOTER_LINKS)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">{meta}
<meta name="theme-color" content="#08070b">
<link rel="icon" href="/static/img/logo-96.webp" type="image/webp">
<link rel="preload" href="/static/fonts/unbounded-latin.woff2" as="font" type="font/woff2" crossorigin>
<title>{html.escape(title)}</title><style>{CSS}{extra_css}</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<div class="wrap">
<header class="site"><a class="brand" href="/"><img src="/static/img/logo-96.webp" alt="" width="40" height="40">AdeleAI&nbsp;<span>Private</span></a>
<nav aria-label="Telegram"><a href="https://t.me/{bot}">Telegram bot</a></nav></header>
<main id="main">{body}</main>
<footer class="site"><span>Payments are processed in crypto through our own payment server.</span>
<nav aria-label="Legal">{links}<a href="https://t.me/{bot}">@{bot}</a></nav></footer>
</div></body></html>""".encode("utf-8")


def _redacted_key():
    return 'ADL-<span class="redact"></span>-<span class="redact"></span>-<span class="redact"></span>'


def _ticket(label, key_html, foot, cls=""):
    return (f'<div class="ticket {cls}">{SEAL}<div class="t-head"><img src="/static/img/logo-256.webp" alt="" width="42" height="42">'
            '<div><div class="t-name">AdeleAI Private</div><div class="t-sub">Access key</div></div></div>'
            f'<p class="t-label">{html.escape(label)}</p><p class="key" style="margin:0">{key_html}</p>'
            f'<div class="foil" aria-hidden="true"></div><p class="t-foot">{html.escape(foot)}</p>{BAND}</div>')


def _summary_css(coins):
    rules = []
    for p in cfg.PLANS:
        rules.append(f'.checkout:has(input[name="plan"][value="{p}"]:checked) [data-plan="{p}"]{{display:inline}}')
    for c in coins:
        rules.append(f'.checkout:has(input[name="coin"][value="{c}"]:checked) [data-coin="{c}"]{{display:inline}}')
    return "".join(rules)


def landing(error=""):
    coins = shkeeper_api.available_coins()
    bot = html.escape(TG_BOT)
    err = f'<p class="notice" role="alert">{html.escape(error)}</p>' if error else ""
    cheapest = min(per_month(eur, days) for eur, days in cfg.PLANS.values())
    if coins:
        terms = "".join(
            f'<label class="term"><input type="radio" name="plan" value="{p}"{" checked" if p == "90" else ""}>'
            f'<span class="term-days">{days} days</span><span class="term-price">€{html.escape(eur)}</span>'
            f'<span class="term-month">€{per_month(eur, days)} per month</span>{icon("check", "tick")}</label>'
            for p, (eur, days) in cfg.PLANS.items())
        tiles = []
        for i, c in enumerate(coins):
            _, _, name, net = COIN_META.get(c, (c, "", cfg.COIN_LABELS.get(c, c), None))
            sub = (f'<span class="coin-net is-net">{html.escape(net)}</span>' if net
                   else f'<span class="coin-net">{html.escape(COIN_META.get(c, (c,))[0])}</span>')
            tiles.append(f'<label class="coin{" has-net" if net else ""}"><input type="radio" name="coin" value="{c}"{" checked" if i == 0 else ""}>'
                         f'{coin_badge(c)}<span class="coin-name">{html.escape(name)}</span>{sub}</label>')
        if any(COIN_META.get(c, (None,) * 4)[3] for c in coins):
            tiles.append(f'<p class="net-note">{icon("spark")}<span>USDT runs on more than one network. Send it on the '
                         'network shown on the tile you pick. A transfer on another network cannot be recovered.</span></p>')
        sum_plan = "".join(f'<strong class="sum-plan" data-plan="{p}">{days} days</strong>' for p, (_, days) in cfg.PLANS.items())
        sum_price = "".join(f'<span class="sum-price" data-plan="{p}">€{html.escape(eur)}</span>' for p, (eur, _) in cfg.PLANS.items())
        sum_coin = "".join(f'<span class="sum-coin" data-coin="{c}">, paid with {html.escape(cfg.COIN_LABELS.get(c, c))}</span>'
                           for c in coins)
        checkout = (f'<form class="checkout" method="post" action="/order">{err}'
                    f'<fieldset><legend>Term</legend><div class="terms">{terms}</div></fieldset>'
                    f'<fieldset><legend>Pay with</legend><div class="coins">{"".join(tiles)}</div></fieldset>'
                    '<div class="checkout-bar"><p class="summary"><span><span class="sum-fallback">Your selection</span>'
                    f'{sum_plan}{sum_coin}</span>{sum_price}</p>'
                    '<button class="cta" type="submit">Continue to payment</button></div>'
                    '<p class="fine">The next page shows the exact amount and a payment address. '
                    'Your key is issued as soon as the payment is confirmed.</p></form>')
    else:
        checkout = (f'<div class="checkout">{err}<p class="notice">Checkout on the website is paused right now. '
                    f'You can still buy in the <a href="https://t.me/{bot}">Telegram bot</a>.</p></div>')
    steps = [("term", "Pick a term and a coin", "You get an address and the exact amount to send."),
             ("wallet", "Pay from any wallet", "Your order page updates by itself and shows your key once the payment is confirmed."),
             ("send", "Redeem the key in Telegram", "The bot checks the key and sends your invitation to the private group.")]
    timeline = "".join(f'<li><span class="step-icon">{icon(ic)}</span><div><span class="step-n">Step {i}</span>'
                       f'<h3>{html.escape(t)}</h3><p>{html.escape(d)}</p></div></li>'
                       for i, (ic, t, d) in enumerate(steps, 1))
    features = "".join(f'<div class="feature">{icon(ic)}<h3>{html.escape(t)}</h3><p>{html.escape(d)}</p></div>'
                       for ic, t, d in INSIDE)
    yes = "".join(f'<li>{icon("check")}<span>{html.escape(t)}</span></li>' for t in INCLUDED)
    no = "".join(f'<li>{icon("minus")}<span>{html.escape(t)}</span></li>' for t in NOT_INCLUDED)
    faq = "".join(f'<details><summary>{html.escape(q)}{icon("plus")}</summary><p>{html.escape(a)}</p></details>' for q, a in FAQ)
    discord = f' or on <a href="{html.escape(DISCORD_INVITE)}">Discord</a>' if DISCORD_INVITE else ""
    ticket = _ticket("Your key appears here after payment", _redacted_key(), "Single use. You redeem it in the Telegram bot.")
    return page("AdeleAI Private", f"""
<section class="hero"><div class="hero-bg" aria-hidden="true"></div><div class="hero-grid">
<div><h1>Your key to AdeleAI Private</h1><p class="lede">{html.escape(TAGLINE)}</p>
<p class="from">From <b>€{cheapest}</b> per month, paid in crypto.</p></div>
{ticket}</div></section>
<section class="block" aria-labelledby="buy-h"><h2 id="buy-h">Get your key</h2>{checkout}</section>
<section class="block" aria-labelledby="how-h"><h2 id="how-h">How it works</h2><ol class="timeline">{timeline}</ol></section>
<section class="block" aria-labelledby="in-h"><h2 id="in-h">Inside the group</h2><div class="features">{features}</div>
<div class="scope"><div class="yes"><h3>Included</h3><ul>{yes}</ul></div><div class="no"><h3>Not included</h3><ul>{no}</ul></div></div></section>
<section class="block faq" aria-labelledby="faq-h"><h2 id="faq-h">Questions</h2>{faq}
<p class="fine">You can also buy directly in the <a href="https://t.me/{bot}">Telegram bot</a>{discord}.</p></section>""",
                extra_css=_summary_css(coins))


STATUS_TEXT = {
    "NEW": ("Creating invoice", "stamp-bad", "This order has no payment address"),
    "PENDING": ("Awaiting payment", "stamp-wait", "Waiting for your payment"),
    "PARTIAL": ("Partly paid", "stamp-wait", "Part of your payment arrived"),
    "PAID": ("Paid", "stamp-ok", "Your key is ready"),
    "REVIEW": ("Under review", "stamp-bad", "Your payment needs a check"),
    "FAILED": ("Invoice failed", "stamp-bad", "This order has no payment address"),
}


def _order_head(order, heading):
    label, css, _ = STATUS_TEXT.get(order["status"], (order["status"], "stamp-bad", ""))
    return (f'<div class="o-meta"><span class="o-no">Order {html.escape(order["order_id"])}</span>'
            f'<span class="stamp {css}">{html.escape(label)}</span></div><h1>{html.escape(heading)}</h1>'
            f'<p class="lede">{order["days"]} days of access for €{html.escape(order["eur"])}</p>')


def order_page(order):
    now = int(time.time())
    coin = html.escape(cfg.COIN_LABELS.get(order["crypto"], order["crypto"]))
    title = f"Order {order['order_id']}"
    heading = STATUS_TEXT.get(order["status"], ("", "", "Your order"))[2]
    bot = html.escape(TG_BOT)
    if order["status"] == "PAID":
        if order["key"]:
            key = html.escape(order["key"])
            body = (_order_head(order, heading)
                    + _ticket("Your access key", f'<span class="key-big key-live">{key}</span>', "Works once. Keep it private.")
                    + f'<a class="cta" href="https://t.me/{bot}?start=key_{key}">Activate in Telegram</a>'
                    f'<p class="fine">You can also paste the key as a message into <a href="https://t.me/{bot}">@{bot}</a>. '
                    'Anyone with the link to this page can see the key.</p>')
        else:
            body = (_order_head(order, "Access added to your Telegram account")
                    + '<p class="fine" style="font-size:1rem">The bot has sent your invitation to the private group.</p>')
        return page(title, f'<div class="order">{body}</div>')
    if order["status"] in ("NEW", "FAILED") or not order["address"]:
        body = (_order_head(order, STATUS_TEXT["FAILED"][2])
                + '<p class="notice" style="margin-top:1.4rem"><a href="/">Start a new order</a> to get a payment address.</p>')
        return page(title, f'<div class="order">{body}</div>')
    notes = ""
    if now > order["expires"] and order["status"] == "PENDING":
        notes += ('<p class="notice" style="margin-top:1.4rem">This amount was calculated more than an hour ago. '
                  'If you have not paid yet, <a href="/">start a new order</a> for a current amount. '
                  'A payment you already sent is still credited.</p>')
    if order["status"] == "PARTIAL":
        notes += ('<p class="notice" style="margin-top:1.4rem">Send the missing amount to the same address. '
                  'Your key is issued when the full amount has arrived.</p>')
    if order["status"] == "REVIEW":
        notes += (f'<p class="notice" style="margin-top:1.4rem">Contact support in <a href="https://t.me/{bot}">the Telegram bot</a> '
                  'and include the order number.</p>')
    body = (_order_head(order, heading) + notes
            + f'<dl class="pay"><dt>Send exactly</dt><dd class="amount">{html.escape(order["amount_crypto"])}<small>{coin}</small></dd>'
            f'<dt>To this address</dt><dd><span class="addr">{html.escape(order["address"])}</span></dd></dl>'
            f'<p class="fine">Send only {coin} on this network. Another coin or network cannot be recovered. '
            'Tap the address once to select it. This page refreshes by itself and shows your key when the payment is confirmed.</p>'
            + _ticket("Your key appears here after payment", _redacted_key(), "Single use. You redeem it in the Telegram bot."))
    return page(title, f'<div class="order">{body}</div>', refresh=20)


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
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
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
