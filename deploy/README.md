# Deploying on Ubuntu (systemd)

`main.py` bootstraps its own virtualenv on first run — no manual `venv`/`pip install`
needed. It creates `./venv`, installs `requirements.txt` into it, then re-execs itself
inside that venv. Because it uses `os.execv()` (not a subprocess fork), the process ID
stays the same throughout, so systemd tracks it correctly as a `Type=simple` service.

## Quick install

```
cd /opt/ticket-tool          # wherever you cloned the repo
cp .env.example .env         # fill in DISCORD_TOKEN etc. before continuing
sudo bash deploy/install.sh
```

That's it. The script:
- writes `/etc/systemd/system/ticket-tool.service`, pointed at this exact directory and
  run as whichever user invoked `sudo` (falls back to `root` if it can't tell)
- installs `ticket-tool-restart.timer` + `.service`, which restarts the bot **daily at
  04:00** (a few minutes of random jitter included so it doesn't line up exactly with
  other scheduled jobs)
- runs `daemon-reload` and enables + starts both units immediately

For a **weekly** restart (every Monday 04:00) instead of daily:

```
sudo bash deploy/install.sh --weekly
```

Re-run `deploy/install.sh` any time to pick up a new project path or user — it's safe to
run repeatedly, it just overwrites the same two unit files.

## Even quicker: let main.py do it for you

Instead of running `install.sh` yourself, set `TICKETBOT_AUTO_SYSTEMD=1` in `.env` and
start the bot as root once:

```
cd /opt/ticket-tool
cp .env.example .env   # fill in DISCORD_TOKEN, set TICKETBOT_AUTO_SYSTEMD=1
sudo python3 main.py
```

On startup it detects it's running as root on Linux and not already under systemd,
runs `deploy/install.sh` for you, then exits the foreground process so systemd's copy
is the only instance running. Safe to leave `TICKETBOT_AUTO_SYSTEMD=1` in `.env`
permanently — every subsequent start under systemd sees `INVOCATION_ID` set (systemd
sets this on every process it manages) and skips straight to running normally, and it
no-ops entirely if you're not root or not on Linux. Set `TICKETBOT_RESTART_SCHEDULE=weekly`
alongside it for a weekly restart instead of daily.

## Checking it worked

```
systemctl status ticket-tool.service
journalctl -u ticket-tool.service -f
systemctl list-timers ticket-tool-restart.timer
```

## Updating the bot

```
cd /opt/ticket-tool
git pull
sudo systemctl restart ticket-tool.service
```

`Data/` (config, tickets, transcripts) lives outside git and is untouched by `git pull`.

## Doing it by hand instead

If you'd rather not run the install script, `ticket-tool.service` in this folder is a
template — replace `__WORKDIR__` with the absolute path to the project and `__USER__`
with the account to run it as, then:

```
sudo cp ticket-tool.service /etc/systemd/system/ticket-tool.service
sudo cp ticket-tool-restart.service ticket-tool-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ticket-tool.service
sudo systemctl enable --now ticket-tool-restart.timer
```

To switch the restart schedule manually, edit `OnCalendar=` in
`/etc/systemd/system/ticket-tool-restart.timer` (`*-*-* 04:00:00` for daily,
`Mon *-*-* 04:00:00` for weekly), then `sudo systemctl daemon-reload && sudo systemctl restart ticket-tool-restart.timer`.
