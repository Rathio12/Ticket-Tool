#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash deploy/install.sh [--weekly]" >&2
  exit 1
fi

SCHEDULE="daily"
if [[ "${1:-}" == "--weekly" ]]; then
  SCHEDULE="weekly"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

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

trap 'notify_discord "❌ Ticket Tool systemd install failed on $(hostname) (exit code $?). Check the install output on the server."' ERR

echo "Installing Ticket Tool as a systemd service"
echo "  project dir : $WORKDIR"
echo "  run as user : $RUN_USER"
echo "  restart     : $SCHEDULE at 04:00"
echo

sed -e "s#__WORKDIR__#${WORKDIR}#g" -e "s#__USER__#${RUN_USER}#g" \
  "$SCRIPT_DIR/ticket-tool.service" > /etc/systemd/system/ticket-tool.service

cp "$SCRIPT_DIR/ticket-tool-restart.service" /etc/systemd/system/ticket-tool-restart.service
cp "$SCRIPT_DIR/ticket-tool-restart.timer" /etc/systemd/system/ticket-tool-restart.timer

if [[ "$SCHEDULE" == "weekly" ]]; then
  sed -i "s/^OnCalendar=.*/OnCalendar=Mon *-*-* 04:00:00/" /etc/systemd/system/ticket-tool-restart.timer
else
  sed -i "s/^OnCalendar=.*/OnCalendar=*-*-* 04:00:00/" /etc/systemd/system/ticket-tool-restart.timer
fi

systemctl daemon-reload
systemctl enable --now ticket-tool.service
systemctl enable --now ticket-tool-restart.timer

trap - ERR
notify_discord "✅ Ticket Tool installed and started as a systemd service on $(hostname) (restart schedule: $SCHEDULE)."

echo
echo "Done. Useful commands:"
echo "  systemctl status ticket-tool.service"
echo "  journalctl -u ticket-tool.service -f"
echo "  systemctl list-timers ticket-tool-restart.timer"
