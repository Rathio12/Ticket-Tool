import traceback
import logging
import discord
from discord.ext import commands
from discord import app_commands
from core.config import OWNER_IDS, BOT_ERROR_CHANNEL_ID
from core.design import Colors, PanelView

log = logging.getLogger(__name__)

class ErrorHandler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        is_owner = interaction.user.id in OWNER_IDS
        msg = f"An unexpected error occurred: `{error}`"
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = "⏳ Command is on cooldown for others, but you can bypass. Try running it again." if is_owner else f"⏳ Command on cooldown. Try again in `{error.retry_after:.1f}s`."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You don't have permission to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "❌ I'm missing required permissions to run this command."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ You don't meet the requirements for this command."

        panel = PanelView(description=msg, color=Colors.ERROR)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=panel, ephemeral=True)
            else:
                await interaction.response.send_message(view=panel, ephemeral=True)
        except Exception:
            pass

        if hasattr(self.bot, "ui"):
            self.bot.ui.append_log("red", "ERROR", f"App cmd error [{getattr(interaction.command, 'name', '?')}]: {error}")

        if not isinstance(error, (app_commands.CommandOnCooldown, app_commands.MissingPermissions,
                                   app_commands.BotMissingPermissions, app_commands.CheckFailure)):
            await self._log_to_channel(interaction.user, getattr(interaction.command, 'name', 'Unknown'), error)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if hasattr(ctx.command, "on_error"):
            return
        if isinstance(error, commands.CommandNotFound):
            return

        is_owner = ctx.author.id in OWNER_IDS

        if isinstance(error, commands.CheckFailure):
            msg = ("❌ Permission Denied: Even as a Bot Owner, you don't meet this specific command requirement."
                   if is_owner else "You do not have permission or meet the requirements to use this command.")
            await ctx.send(view=PanelView(description=msg, color=Colors.ERROR), delete_after=7)
            return

        if isinstance(error, commands.CommandOnCooldown):
            if is_owner:
                await ctx.reinvoke()
                return
            await ctx.send(
                view=PanelView(description=f"⏳ This command is on cooldown. Try again in `{error.retry_after:.1f}s`.", color=Colors.ERROR),
                delete_after=5,
            )
            return

        log.error(f"Command error in !{ctx.command}: {error}", exc_info=error)
        await self._log_to_channel(ctx.author, getattr(ctx.command, 'name', 'Unknown'), error)

    async def _log_to_channel(self, user, command_name, error):
        if not BOT_ERROR_CHANNEL_ID:
            return
        try:
            channel = self.bot.get_channel(BOT_ERROR_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(BOT_ERROR_CHANNEL_ID)
            if channel:
                tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                if len(tb) > 2000:
                    tb = tb[-2000:]
                await channel.send(view=PanelView(
                    title="⚠️ Global Error Tracker",
                    description=(
                        f"An exception was caught in command `!{command_name}`\n\n"
                        f"**User:** {user} (`{user.id}`)\n"
                        f"**Error:**\n```py\n{tb}\n```"
                    ),
                    color=Colors.ERROR,
                    timestamp=True,
                ))
        except Exception as e:
            log.error(f"Failed to log error to channel: {e}")

async def setup(bot):
    await bot.add_cog(ErrorHandler(bot))
