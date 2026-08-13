#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$WORKDIR/.env"

NOTIFY_TOKEN=""
NOTIFY_CHANNEL=""
if [[ -f "$ENV_FILE" ]]; then
  NOTIFY_TOKEN="$(grep -E '^DISCORD_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
  NOTIFY_CHANNEL="$(grep -E '^RESTART_LOG_CHANNEL_ID=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
fi

notify_discord() {
  local message="$1"
  if [[ -z "$NOTIFY_TOKEN" || -z "$NOTIFY_CHANNEL" ]] || ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  local payload
  payload="$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$message" 2>/dev/null)" || return 0
  curl -sS -X POST "https://discord.com/api/v10/channels/${NOTIFY_CHANNEL}/messages" \
    -H "Authorization: Bot ${NOTIFY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null 2>&1 || true
}

trap 'notify_discord "❌ Ticket Tool pm2 install failed on $(hostname) (exit code $?). Check the install output on the server."' ERR

if ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 not found. Install it first: npm install -g pm2" >&2
  exit 1
fi

if grep -qE '^TICKETBOT_AUTO_SYSTEMD=1' "$ENV_FILE" 2>/dev/null; then
  echo "WARNING: TICKETBOT_AUTO_SYSTEMD=1 in .env — set it to 0 before running under pm2," >&2
  echo "         otherwise main.py will also try to install the systemd service on root restarts." >&2
fi

echo "Starting Ticket Tool under pm2"
echo "  project dir : $WORKDIR"
echo

cd "$WORKDIR"
pm2 start deploy/ecosystem.config.js
pm2 save

trap - ERR
notify_discord "✅ Ticket Tool started under pm2 on $(hostname)."

echo
echo "Done. Useful commands:"
echo "  pm2 status"
echo "  pm2 logs TicketV2"
echo "  pm2 restart TicketV2"
echo
echo "Run 'pm2 startup' once (and follow the printed command) so pm2 survives reboots."
