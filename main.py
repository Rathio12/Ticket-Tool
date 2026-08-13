import os
import json
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import discord
from discord.ext import commands
from discord import app_commands

from core.config import DISCORD_TOKEN, BOT_PREFIX, OWNER_IDS, DEV_GUILD_ID

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
async def main():
    setup_discord_logger()
    bot.ui.append_log("", "INFO", "Starting Ticket Tool…")
    bot.ui.append_log("", "INFO", "Connecting to Discord…")
    async with bot:
        await load_cogs()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
