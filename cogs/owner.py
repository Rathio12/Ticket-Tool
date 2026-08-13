import discord
from discord.ext import commands
from core.config import OWNER_IDS
from core.design import Colors, PanelView
import json
from pathlib import Path


class OwnerCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tickets_dir = Path("./Data/tickets")

    async def cog_check(self, ctx: commands.Context):
        is_owner = ctx.author.id in OWNER_IDS
        is_guild_admin = isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator
        if not is_owner and not is_guild_admin:
            await ctx.send("❌ This command is restricted to bot owners and server administrators.")
            return False
        return True

    @commands.command(name="stats", aliases=["serverstats"])
    async def stats(self, ctx: commands.Context):
        guild = ctx.guild
        if not guild:
            return await ctx.send("❌ This command can only be used in a server.")

        guild_dir = self.tickets_dir / str(guild.id)
        total_tickets = open_tickets = closed_tickets = 0
        if guild_dir.exists():
            for ticket_file in guild_dir.glob("*.json"):
                total_tickets += 1
                try:
                    with open(ticket_file, 'r', encoding='utf-8') as f:
                        t = json.load(f)
                        if t.get("status") == "open":
                            open_tickets += 1
                        else:
                            closed_tickets += 1
                except:
                    continue

        joined_at = guild.me.joined_at.strftime("%Y-%m-%d") if guild.me.joined_at else "Unknown"
        thumb = guild.icon.url if guild.icon else None
        await ctx.send(view=PanelView(
            title=f"📊 Analytics for {guild.name}",
            description="Administrative overview for the current server ecosystem.",
            fields=[
                ("🎫 Total Tickets", f"`{total_tickets}`",                            True),
                ("🟢 Open",          f"`{open_tickets}`",                             True),
                ("🔴 Closed",        f"`{closed_tickets}`",                           True),
                ("👑 Server Owner",  f"{guild.owner.mention} (`{guild.owner.id}`)",   False),
                ("👥 Member Count",  f"`{guild.member_count}`",                       True),
                ("📅 Bot Joined",    f"`{joined_at}`",                                True),
            ],
            color=Colors.PRIMARY,
            thumbnail_url=thumb,
            footer=f"Requested by {ctx.author.display_name}",
            timestamp=True,
        ))

    @commands.command(name="botstats", aliases=["globalstats"])
    async def botstats(self, ctx: commands.Context):
        """Bot-operator only: stats across every guild the bot is in."""
        if ctx.author.id not in OWNER_IDS:
            return await ctx.send("❌ This command is restricted to bot owners.")

        total_tickets = open_tickets = closed_tickets = 0
        if self.tickets_dir.exists():
            for guild_dir in self.tickets_dir.iterdir():
                if not guild_dir.is_dir():
                    continue
                for ticket_file in guild_dir.glob("*.json"):
                    total_tickets += 1
                    try:
                        with open(ticket_file, 'r', encoding='utf-8') as f:
                            t = json.load(f)
                            if t.get("status") == "open":
                                open_tickets += 1
                            else:
                                closed_tickets += 1
                    except:
                        continue

        await ctx.send(view=PanelView(
            title="🌐 Global Bot Statistics",
            fields=[
                ("🏠 Guilds",        f"`{len(self.bot.guilds)}`",  True),
                ("🎫 Total Tickets", f"`{total_tickets}`",         True),
                ("🟢 Open",          f"`{open_tickets}`",          True),
                ("🔴 Closed",        f"`{closed_tickets}`",        True),
            ],
            color=Colors.PRIMARY,
            timestamp=True,
        ))

    @commands.command(name="status")
    async def status(self, ctx: commands.Context, *, sub: str = ""):
        if sub.lower() == "tt":
            try:
                lines = self.bot.get_status_lines()
                text = "```\n" + "\n".join(lines) + "\n```"
            except Exception:
                text = "❌ Status data not available yet (bot still starting)."
            await ctx.send(text)
        else:
            await ctx.send("Usage: `!status tt`")

async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerCommands(bot))
