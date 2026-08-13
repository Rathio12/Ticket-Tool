from discord.ext import commands

from core.config import OWNER_IDS
from core.design import Colors, PanelView

_THEME_COLORS = {
    "success": Colors.SUCCESS,
    "error":   Colors.ERROR,
    "warning": Colors.WARNING,
    "gold":    Colors.GOLD,
    "info":    Colors.INFO,
    "neutral": Colors.NEUTRAL,
}


def create_embed(title: str, description: str = "", *, theme: str = "info", **_) -> PanelView:
    return PanelView(title=title, description=description or None,
                      color=_THEME_COLORS.get(theme, Colors.PRIMARY))


def success_embed(title: str, description: str = "", **_) -> PanelView:
    return PanelView(title=title, description=description or None, color=Colors.SUCCESS)


def error_embed(title: str, description: str = "", **_) -> PanelView:
    return PanelView(title=title, description=description or None, color=Colors.ERROR)


def warn_embed(title: str, description: str = "", **_) -> PanelView:
    return PanelView(title=title, description=description or None, color=Colors.WARNING)


def is_owner():
    """Bot-operator check — bypasses everything, for global diagnostic commands only."""
    async def predicate(ctx):
        return ctx.author.id in OWNER_IDS
    return commands.check(predicate)
