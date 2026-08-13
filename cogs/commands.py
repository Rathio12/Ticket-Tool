import discord
from discord.ext import commands
import asyncio
from core.config import OWNER_IDS
from core.design import Colors, PanelView


def _is_privileged(ctx: commands.Context) -> bool:
    if ctx.author.id in OWNER_IDS:
        return True
    if ctx.guild and ctx.author.id == ctx.guild.owner_id:
        return True
    return isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.administrator


class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="sync", hidden=True)
    async def sync_cmd(self, ctx: commands.Context):
        """Manually re-sync global slash commands. Owner-only — global sync can take up to an hour to propagate."""
        if ctx.author.id not in OWNER_IDS:
            return await ctx.send("❌ This command is restricted to bot owners.")
        msg = await ctx.send("⏳ Syncing global commands...")
        try:
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced `{len(synced)}` global command(s). May take up to an hour to appear everywhere.")
        except Exception as e:
            await msg.edit(content=f"❌ Sync failed: `{e}`")

    @commands.command(name="test_limits")
    async def test_limits(self, ctx: commands.Context):
        if not _is_privileged(ctx):
            return await ctx.send("❌ Only the bot owner, server owner, or administrators can run this.")

        await ctx.send(view=PanelView(
            title="🚀 Load Test Started",
            description="Creating test channels until the **500 channel limit** is reached...",
            color=Colors.PRIMARY,
        ))

        category = await ctx.guild.create_category("TestLimitCategory-0")
        created = 0
        i = 0

        while True:
            try:
                await asyncio.sleep(0.6)
                if len(category.channels) >= 50:
                    category = await ctx.guild.create_category(f"TestLimitCategory-{i}")
                await ctx.guild.create_text_channel(f"testlimit-channel-{i}", category=category)
                created += 1
                i += 1
                if created % 25 == 0 and hasattr(self.bot, "ui"):
                    self.bot.ui.append_log("cyan", "INFO", f"test_limits: {created} channels created")

            except discord.HTTPException as e:
                if getattr(e, "code", None) == 30013:
                    await ctx.send(view=PanelView(
                        title="🛑 Limit Reached!",
                        description=f"Hit the maximum server channel limit (500).\n\n**Total created:** `{created}`",
                        color=Colors.DANGER,
                    ))
                    return
                elif getattr(e, "status", None) == 429:
                    await asyncio.sleep(5)
                else:
                    await ctx.send(view=PanelView(
                        description=f"🛑 **Stopped** — Unexpected error: `{e}`\n**Created:** `{created}`",
                        color=Colors.DANGER,
                    ))
                    return
            except Exception as e:
                await ctx.send(view=PanelView(
                    description=f"🛑 **Stopped** — Unknown error: `{e}`\n**Created:** `{created}`",
                    color=Colors.DANGER,
                ))
                return

    @commands.command(name="cleanup_test")
    async def cleanup_test(self, ctx: commands.Context):
        if not _is_privileged(ctx):
            return await ctx.send("❌ Only the bot owner, server owner, or administrators can run this.")

        msg = await ctx.send("⏳ Cleaning up test channels...")
        deleted = 0
        for channel in ctx.guild.channels:
            name_lower = channel.name.lower()
            if name_lower.startswith("testlimit") or name_lower.startswith("test-channel") or name_lower.startswith("test limit category"):
                try:
                    await channel.delete()
                    deleted += 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    if hasattr(self.bot, "ui"):
                        self.bot.ui.append_log("yellow", "WARN", f"cleanup_test: could not delete #{channel.name}: {e}")

        await msg.edit(content=None, view=PanelView(
            title="✅ Cleanup Complete",
            description=f"Safely removed `{deleted}` test channels and categories.",
            color=Colors.SUCCESS,
        ))


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))
