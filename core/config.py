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


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")

OWNER_IDS = _parse_ids(os.getenv("OWNER_IDS", ""))

DEV_GUILD_ID = _parse_id(os.getenv("DEV_GUILD_ID", ""))

BOT_ERROR_CHANNEL_ID = _parse_id(os.getenv("BOT_ERROR_CHANNEL_ID", ""))
RESTART_LOG_CHANNEL_ID = _parse_id(os.getenv("RESTART_LOG_CHANNEL_ID", ""))

PRESENCE_TEXT = os.getenv("PRESENCE_TEXT", "for /ticket setup")

DATA_DIR = _BASE / "Data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
