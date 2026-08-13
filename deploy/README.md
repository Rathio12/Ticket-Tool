# Deploying on Ubuntu (systemd)

`main.py` bootstraps its own virtualenv on first run — no manual `venv`/`pip install`
needed. It creates `./venv`, installs `requirements.txt` into it, then re-execs itself
inside that venv. Because it uses `os.execv()` (not a subprocess fork), the process ID
stays the same throughout, so systemd tracks it correctly as a `Type=simple` service.

## 1. Copy the project to the server

```
sudo mkdir -p /opt/ticket-tool
sudo cp -r . /opt/ticket-tool
cd /opt/ticket-tool
cp .env.example .env   # then fill in DISCORD_TOKEN etc.
```

## 2. Install the service

Edit `ticket-tool.service` first if your path or user differs from the defaults
(`/opt/ticket-tool`, run as your own user via `User=%i`).

```
sudo cp deploy/ticket-tool.service /etc/systemd/system/ticket-tool@.service
sudo systemctl daemon-reload
sudo systemctl enable --now ticket-tool@youruser.service
```

(The `@` template lets one unit file run as any user — swap `youruser` for the actual
account, or simplify by hardcoding `User=` in the unit and dropping the `@`.)

Check it's alive:

```
sudo systemctl status ticket-tool@youruser.service
journalctl -u ticket-tool@youruser.service -f
```

## 3. Scheduled restart (daily at 4am)

Long-running bots benefit from a periodic clean restart (clears any slow memory creep,
picks up host-level changes). This repo ships a `systemd` timer for it instead of cron —
it survives reboots and logs to the same place as everything else.

```
sudo cp deploy/ticket-tool-restart.service /etc/systemd/system/
sudo cp deploy/ticket-tool-restart.timer /etc/systemd/system/
```

If you templated the main service as `ticket-tool@youruser.service`, update the
`ExecStart=` line in `ticket-tool-restart.service` to match before copying it over.

```
sudo systemctl daemon-reload
sudo systemctl enable --now ticket-tool-restart.timer
```

Verify the schedule:

```
systemctl list-timers ticket-tool-restart.timer
```

### Daily vs. weekly

The shipped timer restarts **daily at 04:00** (with a random 0–5 min jitter so it doesn't
line up exactly with other cron/timer jobs). For a **weekly** restart instead — e.g. every
Monday at 4am — edit the `OnCalendar=` line in `ticket-tool-restart.timer`:

```
OnCalendar=Mon *-*-* 04:00:00
```

Then `sudo systemctl daemon-reload && sudo systemctl restart ticket-tool-restart.timer`.

## Updating the bot

```
cd /opt/ticket-tool
git pull
sudo systemctl restart ticket-tool@youruser.service
```

`Data/` (config, tickets, transcripts) lives outside git and is untouched by `git pull`.
