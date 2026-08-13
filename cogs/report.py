import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

from core.design import Colors, PanelView, BRAND_NAME

DATA_DIR   = Path(__file__).resolve().parent.parent / "Data"
TICKET_DIR = DATA_DIR / "tickets"
REPORT_DIR = DATA_DIR / "reports"

def _report_path(guild_id: int) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR / f"{guild_id}.json"


def _load_anchor(guild_id: int) -> dict | None:
    p = _report_path(guild_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_anchor(guild_id: int, channel_id: int, message_id: int):
    _report_path(guild_id).write_text(
        json.dumps({"channel_id": channel_id, "message_id": message_id}),
        encoding="utf-8",
    )


def _delete_anchor(guild_id: int):
    p = _report_path(guild_id)
    if p.exists():
        p.unlink()


def _collect_stats(guild_id: int) -> dict:
    guild_dir = TICKET_DIR / str(guild_id)
    if not guild_dir.is_dir():
        return {}

    now = datetime.now(timezone.utc)
    today_start   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start    = today_start - timedelta(days=today_start.weekday())

    total_open = total_closed = 0
    opened_today = closed_today = 0
    closed_this_week = 0
    staff_counts: dict[int, int] = defaultdict(int)
    avg_close_times: list[float] = []
    avg_response_times: list[float] = []
    sla_breaches = 0

    for f in guild_dir.iterdir():
        if f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        status       = data.get("status")
        created_at   = data.get("created_at")
        closed_at    = data.get("closed_at")
        claimant     = data.get("claimant_id")
        responded_at = data.get("first_response_at")

        if status == "open":
            total_open += 1
        elif status == "closed":
            total_closed += 1

        if created_at:
            try:
                dt = datetime.fromisoformat(created_at)
                if dt >= today_start:
                    opened_today += 1
            except Exception:
                pass

        if status == "closed" and closed_at:
            try:
                closed_dt = datetime.fromisoformat(closed_at)
                if closed_dt >= today_start:
                    closed_today += 1
                if closed_dt >= week_start:
                    closed_this_week += 1
                if created_at:
                    created_dt = datetime.fromisoformat(created_at)
                    diff = (closed_dt - created_dt).total_seconds() / 60
                    if diff >= 0:
                        avg_close_times.append(diff)
            except Exception:
                pass

        if created_at and responded_at:
            try:
                created_dt = datetime.fromisoformat(created_at)
                responded_dt = datetime.fromisoformat(responded_at)
                diff = (responded_dt - created_dt).total_seconds() / 60
                if diff >= 0:
                    avg_response_times.append(diff)
            except Exception:
                pass
        elif status == "closed" and not responded_at:
            sla_breaches += 1

        if claimant:
            staff_counts[claimant] += 1

    top_staff = sorted(staff_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    avg_mins  = round(sum(avg_close_times) / len(avg_close_times)) if avg_close_times else None
    avg_response_mins = round(sum(avg_response_times) / len(avg_response_times)) if avg_response_times else None

    return {
        "total_open":       total_open,
        "total_closed":     total_closed,
        "opened_today":     opened_today,
        "closed_today":     closed_today,
        "closed_this_week": closed_this_week,
        "top_staff":        top_staff,
        "avg_close_mins":   avg_mins,
        "avg_response_mins": avg_response_mins,
        "sla_breaches":     sla_breaches,
        "generated_at":     now,
    }


_MEDALS = ["🥇", "🥈", "🥉", "④", "⑤"]


def _fmt_mins(mins: int) -> str:
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _build_panel(guild: discord.Guild, stats: dict) -> PanelView:
    now: datetime   = stats["generated_at"]
    total           = stats["total_open"] + stats["total_closed"]
    resolution_rate = round((stats["total_closed"] / total) * 100) if total else 0
    avg_str         = _fmt_mins(stats["avg_close_mins"]) if stats["avg_close_mins"] is not None else "—"
    avg_response_str = _fmt_mins(stats["avg_response_mins"]) if stats["avg_response_mins"] is not None else "—"
    filled          = round(resolution_rate / 10)
    bar             = "█" * filled + "░" * (10 - filled)
    thumb           = guild.icon.url if guild.icon else None

    fields = [
        ("🎫  All Time",
         f"```\nOpen     {stats['total_open']:>6}\nClosed   {stats['total_closed']:>6}\nTotal    {total:>6}\n```",
         True),
        ("📅  Today",
         f"```\nOpened   {stats['opened_today']:>6}\nClosed   {stats['closed_today']:>6}\n```",
         True),
        ("📆  This Week",
         f"```\nClosed   {stats['closed_this_week']:>6}\nAvg time {avg_str:>6}\n```",
         True),
        ("📈  Resolution Rate",
         f"`{bar}` **{resolution_rate}%** of all tickets resolved",
         False),
        ("⏱️  Avg First Response",
         f"`{avg_response_str}`  —  `{stats['sla_breaches']}` closed with no staff reply",
         False),
    ]
    if stats["top_staff"]:
        lines = "\n".join(
            f"{_MEDALS[i]}  <@{uid}>  —  `{count}` ticket{'s' if count != 1 else ''}"
            for i, (uid, count) in enumerate(stats["top_staff"])
        )
        fields.append(("🏆  Top Staff  *(all time)*", lines, False))
    else:
        fields.append(("🏆  Top Staff", "*No ticket data yet.*", False))

    return PanelView(
        title=f"📊 {guild.name}  •  Support Report",
        description=(
            f"Live statistics for **{guild.name}** support operations.\n"
            f"-# Auto-refreshes every 24h  •  Last updated {discord.utils.format_dt(now, 'R')}"
        ),
        fields=fields,
        color=Colors.PRIMARY,
        thumbnail_url=thumb,
        footer=f"{BRAND_NAME}  •  This message is persistent and survives restarts",
    )


class ReportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.report_loop.start()

    def cog_unload(self):
        self.report_loop.cancel()

    async def _get_report_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        anchor = _load_anchor(guild.id)
        if not anchor:
            return None
        ch = self.bot.get_channel(anchor["channel_id"])
        if ch:
            return ch
        try:
            return await self.bot.fetch_channel(anchor["channel_id"])
        except Exception:
            return None

    async def _update_or_create(self, guild: discord.Guild):
        stats = _collect_stats(guild.id)
        if not stats:
            return
        anchor = _load_anchor(guild.id)
        if not anchor:
            return
        panel   = _build_panel(guild, stats)
        channel = await self._get_report_channel(guild)

        if not channel:
            if hasattr(self.bot, "ui"):
                self.bot.ui.append_log("yellow", "WARN", f"report: configured channel unreachable for guild {guild.id}")
            return

        if anchor.get("message_id"):
            try:
                msg = await channel.fetch_message(anchor["message_id"])
                await msg.edit(view=panel)
                if hasattr(self.bot, "ui"):
                    self.bot.ui.append_log("cyan", "INFO", f"report: updated live message in {guild.name}")
                return
            except discord.NotFound:
                if hasattr(self.bot, "ui"):
                    self.bot.ui.append_log("yellow", "WARN", f"report: message deleted in {guild.name}, re-creating")
            except Exception as e:
                if hasattr(self.bot, "ui"):
                    self.bot.ui.append_log("yellow", "WARN", f"report: edit failed ({e}), resending in {guild.name}")
                try:
                    old_msg = await channel.fetch_message(anchor["message_id"])
                    await old_msg.delete()
                except Exception:
                    pass

        try:
            msg = await channel.send(view=panel)
            _save_anchor(guild.id, channel.id, msg.id)
            if hasattr(self.bot, "ui"):
                self.bot.ui.append_log("green", "OK", f"report: created live message in {guild.name}")
        except Exception as e:
            if hasattr(self.bot, "ui"):
                self.bot.ui.append_log("red", "ERR", f"report: send failed in {guild.name}: {e}")

    @tasks.loop(hours=24)
    async def report_loop(self):
        for guild in self.bot.guilds:
            await self._update_or_create(guild)

    @report_loop.before_loop
    async def before_report_loop(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self._update_or_create(guild)

    @commands.command(name="report", hidden=True)
    @commands.has_permissions(administrator=True)
    async def force_report(self, ctx: commands.Context):
        anchor = _load_anchor(ctx.guild.id)
        if not anchor:
            return await ctx.send("❌ No report channel configured yet. Run `!report_channel #channel` first.")
        await ctx.message.add_reaction("⏳")
        await self._update_or_create(ctx.guild)
        await ctx.message.add_reaction("✅")

    @commands.command(name="report_channel", hidden=True)
    @commands.has_permissions(administrator=True)
    async def set_report_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        anchor = _load_anchor(ctx.guild.id)
        _save_anchor(ctx.guild.id, channel.id, anchor["message_id"] if anchor else 0)
        await ctx.send(f"✅ Report channel set to {channel.mention}. Run `!report` to post now.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportCog(bot))
