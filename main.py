import os
import json
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _bootstrap_venv():
    import subprocess
    root = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(root, "venv")
    bindir = "Scripts" if os.name == "nt" else "bin"
    vpy = os.path.join(venv_dir, bindir, "python.exe" if os.name == "nt" else "python")

    if os.path.abspath(sys.prefix) == os.path.abspath(venv_dir):
        return
    if os.environ.get("_TICKETBOT_BOOTSTRAPPED") == "1" or os.environ.get("TICKETBOT_NO_BOOTSTRAP") == "1":
        return

    fresh = not os.path.exists(vpy)
    try:
        if fresh:
            print("[bootstrap] No venv found — creating one…", flush=True)
            subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        req = os.path.join(root, "requirements.txt")
        if os.path.exists(req) and (fresh or os.environ.get("TICKETBOT_INSTALL_DEPS") == "1"):
            print("[bootstrap] Installing requirements…", flush=True)
            subprocess.check_call([vpy, "-m", "pip", "install", "--upgrade", "pip", "-q"])
            subprocess.check_call([vpy, "-m", "pip", "install", "-q", "-r", req])
    except Exception as exc:
        print(f"[bootstrap] setup failed ({exc}); continuing with current interpreter.", flush=True)
        return

    os.environ["_TICKETBOT_BOOTSTRAPPED"] = "1"
    cmd = [vpy, os.path.abspath(__file__), *sys.argv[1:]]
    if os.name == "nt":
        print("[bootstrap] Launching inside venv…", flush=True)
        raise SystemExit(subprocess.call(cmd))
    try:
        bindir_path = os.path.dirname(vpy)
        for f in os.listdir(bindir_path):
            fp = os.path.join(bindir_path, f)
            if os.path.isfile(fp):
                os.chmod(fp, 0o755)
    except OSError:
        pass
    print("[bootstrap] Launching inside venv…", flush=True)
    os.execv(vpy, cmd)


_bootstrap_venv()

import discord
from discord.ext import commands
from discord import app_commands

from core.config import DISCORD_TOKEN, BOT_PREFIX, OWNER_IDS, DEV_GUILD_ID, RESTART_LOG_CHANNEL_ID
from core.design import Colors, PanelView

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN not set in .env")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.messages = True
intents.reactions = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)
bot.owner_ids = set(OWNER_IDS)

# ---------------------------------------------------------------------------
# CLI logger
# ---------------------------------------------------------------------------

_ANSI = {
    "OK":   "\033[32m",   # green
    "INFO": "\033[36m",   # cyan
    "WARN": "\033[33m",   # yellow
    "FAIL": "\033[31m",   # red
    "ERR":  "\033[31m",   # red
    "RESET":"\033[0m",
    "DIM":  "\033[2m",
}


class TerminalUI:
    def append_log(self, _color: str, level: str, message: str):
        ts    = datetime.now().strftime("%H:%M:%S")
        col   = _ANSI.get(level, "")
        reset = _ANSI["RESET"]
        dim   = _ANSI["DIM"]
        print(f"{dim}{ts}{reset} {col}[{level:<4}]{reset} {message}", flush=True)


bot.ui = TerminalUI()


def _count_commands(cmds=None) -> int:
    if cmds is None:
        cmds = bot.tree.get_commands()
    total = 0
    for cmd in cmds:
        if isinstance(cmd, app_commands.Group):
            total += _count_commands(cmd.commands)
        else:
            total += 1
    return total


# ---------------------------------------------------------------------------
# File logger
# ---------------------------------------------------------------------------
_file_log = logging.getLogger("file_log")
_file_log.setLevel(logging.DEBUG)
_fh = logging.FileHandler("bot_debug.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_file_log.addHandler(_fh)
bot.file_log = _file_log

bot_start_time = datetime.now(timezone.utc)
DATA_DIR = Path(__file__).parent / "Data"
DATA_DIR.mkdir(exist_ok=True)
RESTART_STATE_PATH = DATA_DIR / "restart_state.json"


def _read_restart_state() -> dict:
    try:
        return json.loads(RESTART_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_restart_state(status: str, **extra):
    try:
        payload = {"status": status, "at": datetime.now(timezone.utc).isoformat(), **extra}
        RESTART_STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


async def _send_to_restart_channel(**panel_kwargs):
    if not RESTART_LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(RESTART_LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(RESTART_LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(view=PanelView(**panel_kwargs))
    except Exception:
        pass


def _recent_error_lines(limit: int = 8) -> str:
    path = Path("debug.log")
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-limit:])
    except Exception:
        return ""


async def _report_startup_state():
    prev = _read_restart_state()
    status = prev.get("status")
    if status == "clean_shutdown":
        await _send_to_restart_channel(
            title="✅ Restart complete",
            description="The bot shut down cleanly and is back online.",
            color=Colors.SUCCESS,
            timestamp=True,
        )
    elif status == "running":
        tail = _recent_error_lines()
        fields = [("Last logged errors", f"```\n{tail}\n```", False)] if tail else []
        await _send_to_restart_channel(
            title="⚠️ Restarted after an unclean shutdown",
            description="The bot came back online, but the previous run did not exit cleanly (crash, OOM kill, or a forced stop).",
            fields=fields,
            color=Colors.WARNING,
            timestamp=True,
        )
    _write_restart_state("running")


def _count_tickets_from_json():
    open_cnt = closed_cnt = 0
    guild_dirs = DATA_DIR / "tickets"
    if not guild_dirs.is_dir():
        return 0, 0
    for gd in guild_dirs.iterdir():
        if not gd.is_dir():
            continue
        for f in gd.iterdir():
            if f.suffix != ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("status") == "open":
                    open_cnt += 1
                elif data.get("status") == "closed":
                    closed_cnt += 1
            except Exception:
                pass
    return open_cnt, closed_cnt


_ticket_cache: tuple[int, int] = (0, 0)
_ticket_cache_ts: float = 0.0


def _count_tickets_cached() -> tuple[int, int]:
    import time
    global _ticket_cache, _ticket_cache_ts
    now = time.monotonic()
    if now - _ticket_cache_ts < 10:
        return _ticket_cache
    _ticket_cache = _count_tickets_from_json()
    _ticket_cache_ts = now
    return _ticket_cache


bot.get_ticket_counts = _count_tickets_cached


def _get_status_lines() -> list[str]:
    open_cnt, closed_cnt = _count_tickets_cached()
    uptime = datetime.now(timezone.utc) - bot_start_time
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    latency_ms = round(bot.latency * 1000) if bot.latency else 0
    return [
        f"Guilds:       {len(bot.guilds)}",
        f"Users:        {sum(g.member_count or 0 for g in bot.guilds)}",
        f"Open tickets: {open_cnt}",
        f"Closed:       {closed_cnt}",
        f"Latency:      {latency_ms}ms",
        f"Uptime:       {hours}h {minutes}m {seconds}s",
    ]


bot.get_status_lines = _get_status_lines

# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    bot.ui.append_log("", "OK", f"Discord connected as {bot.user} ({bot.user.id})")

    if not getattr(bot, "_startup_reported", False):
        bot._startup_reported = True
        await _report_startup_state()

    try:
        all_local   = bot.tree.get_commands()
        total_local = _count_commands(all_local)
        names = ", ".join(f"/{c.name}" for c in all_local)
        bot.ui.append_log("", "INFO", f"Commands ready: {names} ({total_local} total)")

        if DEV_GUILD_ID:
            # Fast local sync while developing — mirrors global commands to one guild instantly.
            guild_obj = discord.Object(id=DEV_GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
            bot.ui.append_log("", "OK", f"Synced {total_local} commands → dev guild ({DEV_GUILD_ID})")
        else:
            synced = await bot.tree.sync()
            bot.ui.append_log("", "OK", f"Synced {len(synced)} commands globally (installable on any server)")

    except Exception as e:
        bot.ui.append_log("", "FAIL", f"Sync failed: {e}")

    bot.ui.append_log("", "INFO", f"Serving {len(bot.guilds)} guild(s)")


@bot.event
async def on_guild_join(guild: discord.Guild):
    bot.ui.append_log("", "OK", f"Joined guild: {guild.name} ({guild.id}) — {guild.member_count} members")


@bot.event
async def on_command_error(ctx, error):
    # cogs/errorhandler.py handles this fully; keep a bare fallback so
    # CommandNotFound never spams the console if that cog fails to load.
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

# ---------------------------------------------------------------------------
# Cog loader
# ---------------------------------------------------------------------------
async def load_cogs():
    for filename in sorted(os.listdir("./cogs")):
        if filename.endswith(".py"):
            cog = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(cog)
                bot.ui.append_log("", "OK", f"Loaded  {cog}")
            except Exception as e:
                bot.ui.append_log("", "FAIL", f"Failed  {cog} — {e}")


def setup_discord_logger(log_filename="bot_debug.log"):
    logging.basicConfig(
        filename=log_filename,
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("discord.client").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)

    error_handler = logging.FileHandler("debug.log", encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(error_handler)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _graceful_shutdown():
    bot.ui.append_log("", "WARN", "Shutdown signal received — closing cleanly...")
    if bot.is_ready():
        await _send_to_restart_channel(
            title="🔁 Restarting",
            description="A restart was requested. The bot is shutting down cleanly and will be back shortly.",
            color=Colors.WARNING,
            timestamp=True,
        )
    _write_restart_state("clean_shutdown")
    await bot.close()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop):
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_graceful_shutdown()))
        except (NotImplementedError, AttributeError):
            pass


async def main():
    setup_discord_logger()
    bot.ui.append_log("", "INFO", "Starting Ticket Tool…")
    bot.ui.append_log("", "INFO", "Connecting to Discord…")
    _install_signal_handlers(asyncio.get_running_loop())
    async with bot:
        await load_cogs()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
