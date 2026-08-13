import os
from pathlib import Path
from dotenv import load_dotenv

_BASE = Path(__file__).resolve().parent.parent
load_dotenv(_BASE / ".env")


def _parse_ids(raw: str) -> list[int]:
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def _parse_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


# ── Secrets / core ────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")

# ── Bot operators (global, not per-guild) ──────────────────────
# Bypass staff checks everywhere and can use owner-only diagnostic commands.
OWNER_IDS = _parse_ids(os.getenv("OWNER_IDS", ""))

# Optional: instantly sync slash commands to one guild while developing.
# Leave unset in production — global sync (no guild) is what makes the bot
# installable on any server without code changes.
DEV_GUILD_ID = _parse_id(os.getenv("DEV_GUILD_ID", ""))

# Optional: a single channel (in the bot operator's own server) that receives
# uncaught errors that can't be attributed to a specific guild's error channel.
BOT_ERROR_CHANNEL_ID = _parse_id(os.getenv("BOT_ERROR_CHANNEL_ID", ""))
RESTART_LOG_CHANNEL_ID = _parse_id(os.getenv("RESTART_LOG_CHANNEL_ID", ""))

PRESENCE_TEXT = os.getenv("PRESENCE_TEXT", "for /ticket setup")

# ── Paths ───────────────────────────────────────────────────────
DATA_DIR = _BASE / "Data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
