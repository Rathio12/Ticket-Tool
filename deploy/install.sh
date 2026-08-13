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

echo
echo "Done. Useful commands:"
echo "  systemctl status ticket-tool.service"
echo "  journalctl -u ticket-tool.service -f"
echo "  systemctl list-timers ticket-tool-restart.timer"
