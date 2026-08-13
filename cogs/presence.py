import discord
from discord.ext import commands, tasks

from core.config import PRESENCE_TEXT


class PresenceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._idx = 0
        self.presence_task.start()

    def cog_unload(self):
        self.presence_task.cancel()

    def _rotation(self) -> list[tuple[discord.ActivityType, str]]:
        guild_count = len(self.bot.guilds)
        open_count, closed_count = (0, 0)
        if hasattr(self.bot, "get_ticket_counts"):
            try:
                open_count, closed_count = self.bot.get_ticket_counts()
            except Exception:
                pass
        return [
            (discord.ActivityType.watching, PRESENCE_TEXT),
            (discord.ActivityType.watching, f"{guild_count} server{'s' if guild_count != 1 else ''}"),
            (discord.ActivityType.playing, f"{open_count} open ticket{'s' if open_count != 1 else ''}"),
        ]

    @tasks.loop(seconds=20)
    async def presence_task(self):
        try:
            rotation = self._rotation()
            activity_type, name = rotation[self._idx % len(rotation)]
            self._idx += 1
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=activity_type, name=name),
            )
        except Exception as e:
            if hasattr(self.bot, "ui"):
                self.bot.ui.append_log("", "WARN", f"presence: update failed: {e}")

    @presence_task.before_loop
    async def before_presence_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(PresenceCog(bot))
