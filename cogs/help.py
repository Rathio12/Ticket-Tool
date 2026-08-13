import discord
from discord import app_commands
from discord.ext import commands

from core.design import Colors, PanelView


def _walk_commands(cmds, prefix=""):
    lines = []
    for cmd in sorted(cmds, key=lambda c: c.name):
        if isinstance(cmd, app_commands.Group):
            lines.extend(_walk_commands(cmd.commands, prefix + cmd.name + " "))
        else:
            lines.append(f"`/{prefix}{cmd.name}` — {cmd.description}")
    return lines


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="List every available command.")
    async def help_cmd(self, interaction: discord.Interaction):
        lines = _walk_commands(self.bot.tree.get_commands())
        description = "\n".join(lines) if lines else "No commands registered yet."
        await interaction.response.send_message(view=PanelView(
            title="📖 Commands",
            description=description,
            color=Colors.PRIMARY,
        ), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
