# AdeleAI Telegram bot

Python 3.10 or newer is required. Set `BOT_TOKEN`, `PRIVATE_CHANNEL_ID`, and `CRYPTO_PAY_TOKEN` in the environment, then run `python bot.py`. The bot uses SQLite in its working directory. Keep the database and tokens private and back up the database. Only one bot process may poll Telegram at a time.

## Website and Shkeeper payments

Sales run through three front ends that share one SQLite database and one access module (`access.py`):

- **Website** (`web.py`, adeleai.org): plan and coin selection, order status page, Shkeeper callback at `/shkeeper/callback`.
- **Telegram bot** (`bot.py`): Shkeeper coins plus the existing Crypto Bot option.
- **Discord bot** (`discord_sales.py`): Shkeeper coins. The old LTC xpub path is used only when Shkeeper is not configured.

Access is always the private Telegram group. Telegram orders extend the buyer's subscription directly. Website and
Discord orders produce a single-use `ADL-` key, which the buyer redeems in the Telegram bot by pasting it or through
the link `https://t.me/<bot>?start=key_<KEY>`. The website's callback handler grants access; the bots only send the
invitation or key, so each bot token is still polled by one process only.

Shkeeper endpoints, coin names and the callback format, with their source commit, are in `payment_config.py`.

### Shkeeper server requirements (from the Helm chart defaults, check before installing)

| Coin | Shkeeper name | Backend it needs |
|---|---|---|
| BTC | `BTC` | Default points to the vendor's public node; a local node is optional (`btc_fullnode`) |
| LTC | `LTC` | Default points to the vendor's public node; a local node is optional (`ltc_fullnode`) |
| ETH, USDT ERC20 | `ETH`, `ETH-USDT` | An Ethereum RPC endpoint you provide (`eth_fullnode.url`) |
| SOL, USDT SPL | `SOL`, `SOLANA-USDT` | A Solana RPC endpoint you provide (`solana_fullnode.url`) |
| USDT TRC20 | `USDT` | A Tron node (`tron_fullnode`); not enabled by default here |

Shkeeper installs with k3s and Helm. A full Ethereum or Solana node needs far more disk and RAM than a small shared
server has; use an RPC provider or a separate server for Shkeeper.

### Environment

Copy `deploy/web.env.example` to `/etc/adele/web.env` (mode 600). The bot and Discord services need the same
`DATABASE_PATH`, `SHKEEPER_URL`, `SHKEEPER_API_KEY`, `SHKEEPER_COINS` and `PUBLIC_BASE_URL` values.

| Variable | Meaning |
|---|---|
| `SHKEEPER_URL` | Base URL of your Shkeeper, without `/api` |
| `SHKEEPER_API_KEY` | Shkeeper API key; it is also the webhook signing secret |
| `SHKEEPER_COINS` | Coins offered, Shkeeper names, comma-separated |
| `SHKEEPER_MIN_PAID_RATIO` | Must match the Shkeeper wallet's lower limit (default 95 % = `0.95`) |
| `PUBLIC_BASE_URL` | `https://adeleai.org`; Shkeeper must be able to reach `<this>/shkeeper/callback` |
| `WEB_PORT` | Local port for `web.py`; must be free and match `deploy/caddy/adeleai.org.caddy` |
| `TELEGRAM_BOT_USERNAME`, `DISCORD_INVITE_URL` | Links on the website |

### Setup order

1. Back up the database: `sqlite3 adeleai.sqlite3 ".backup adeleai-backup.sqlite3"`. The new `orders` table is
   added with `CREATE TABLE IF NOT EXISTS`; existing tables are not changed.
2. Install and configure Shkeeper, enable the wallets for the coins above, copy the API key.
3. Put the env files in place, install `deploy/systemd/adele-web.service`, then `systemctl enable --now adele-web`.
4. Install `deploy/nginx/adeleai.org.conf` into nginx (commands are at the top of that file), then run certbot for HTTPS.
5. Restart the Telegram bot and the Discord bot so they load the new code and settings.
6. For auto-deploy, set the repository variable `DEPLOY_EXTRA_SERVICES` (for example `adele-web adele-discord`).

Tests: `python3 -m unittest discover -s tests`.
