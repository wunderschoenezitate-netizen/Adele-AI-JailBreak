#!/usr/bin/env bash
# Pulls origin/$BRANCH into $APP_DIR; if the code changed and compiles, restarts $SERVICE.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/adele-bot}"
SERVICE="${SERVICE:-adele-bot}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"
old=$(git rev-parse HEAD)
new=$(git rev-parse "origin/$BRANCH")
[ "$old" = "$new" ] && exit 0

git reset --hard "origin/$BRANCH"
if ! python3 -m py_compile bot.py discord_sales.py addresses.py; then
    git reset --hard "$old"
    echo "update to $new failed to compile; kept $old" >&2
    exit 1
fi
rm -rf __pycache__
systemctl restart "$SERVICE"
echo "updated $old -> $new, restarted $SERVICE"
