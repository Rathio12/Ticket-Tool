"""
Standalone Discord Components V2 design system — portable across projects.

No bot-specific imports beyond stdlib `os`, no external config file required.
Drop this file into any discord.py project and import `PanelView` / `Colors` /
`format_panel_fields`.

Components V2 in one paragraph: instead of `discord.Embed`, Discord now lets
you build a message out of layout components — Container (a colored card),
Section (up to 3 Text Displays + one accessory, which is either a Thumbnail
image or a Button), TextDisplay (markdown text), Separator (a thin divider),
and ActionRow (a row of buttons). You build these with `discord.ui.*` classes
and send them via `channel.send(view=my_layout_view)` — never `embed=`.
discord.py automatically sets the required message flag for you the moment
any Components V2 item is present in the view (see `has_components_v2()` /
`MessageFlags.components_v2` in discord/http.py if you want to verify this
yourself) — you never need to set flags manually.

Set the BRAND_NAME env var to rebrand every panel/entry footer for your own
deployment — it's the only project-specific thing in this file.
"""
from __future__ import annotations

import os
import discord
from discord import ui
from datetime import datetime, timezone
from typing import Optional

BRAND_NAME = os.environ.get("BRAND_NAME", "Ticket Tool")


class Colors:
    PRIMARY  = 0x5865F2
    SUCCESS  = 0x57F287
    WARNING  = 0xFEE75C
    DANGER   = 0xED4245
    ERROR    = 0xED4245
    INFO     = 0x3498DB
    MODERATE = 0xE67E22
    NEUTRAL  = 0x2B2D31
    PINK     = 0xEB459E
    PURPLE   = 0x9B59B6
    TEAL     = 0x1ABC9C
    GOLD     = 0xF1C40F


def format_panel_fields(fields: list) -> list:
    """Turn (name, value, inline) triples into body paragraphs — the closest
    text-only equivalent to how embed fields with inline=True sit two/three
    per row, since Components V2 has no real field grid. Consecutive
    inline=True fields are paired two-per-line; everything else (inline=False,
    or an inline field with no inline neighbor left) gets its own paragraph.
    """
    out = []
    i = 0
    while i < len(fields):
        name, value, inline = fields[i]
        if inline and i + 1 < len(fields) and fields[i + 1][2]:
            name2, value2, _ = fields[i + 1]
            out.append(f"**{name}:** {value}    **{name2}:** {value2}")
            i += 2
        else:
            out.append(f"**{name}**\n{value}")
            i += 1
    return out


class PanelView(ui.LayoutView):
    """Generic Components V2 info panel — drop-in replacement for discord.Embed.

    Send with `await channel.send(view=view)` — never `embed=`.
    """

    def __init__(
        self,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        fields: Optional[list] = None,
        color: int = Colors.PRIMARY,
        thumbnail_url: Optional[str] = None,
        footer: Optional[str] = None,
        timestamp: bool = False,
        timeout: Optional[float] = None,
    ):
        super().__init__(timeout=timeout)
        fields = fields or []

        header_lines = []
        if title:
            header_lines.append(f"# {title}")
        if description:
            header_lines.append(description)

        body_blocks = format_panel_fields(fields)
        children = []

        if thumbnail_url and header_lines:
            slots_left = 3 - len(header_lines[:3])
            head = [ui.TextDisplay(t) for t in header_lines[:3]]
            in_section, overflow = body_blocks[:slots_left], body_blocks[slots_left:]
            children.append(ui.Section(
                *head, *(ui.TextDisplay(b) for b in in_section),
                accessory=ui.Thumbnail(media=thumbnail_url),
            ))
            children.extend(ui.TextDisplay(b) for b in overflow)
        else:
            if header_lines:
                children.append(ui.TextDisplay("\n\n".join(header_lines)))
            children.extend(ui.TextDisplay(b) for b in body_blocks)

        if not children:
            children.append(ui.TextDisplay("​"))

        ts_part = f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>" if timestamp else ""
        extra = f"  •  {footer}" if footer else ""
        ts_sep = f"  •  {ts_part}" if ts_part else ""
        children.append(ui.Separator(spacing=discord.SeparatorSpacing.small))
        children.append(ui.TextDisplay(f"-# {BRAND_NAME}{extra}{ts_sep}"))

        container = ui.Container(*children, accent_colour=color)
        self.add_item(container)


class EntryView(ui.LayoutView):
    """Components V2 layout for a single log/audit-style entry.

    Use for "X did Y to Z, here's why" messages — mod actions, ticket logs, etc.
    """

    def __init__(
        self,
        *,
        emoji: str,
        title: str,
        target_display: str,
        actor_display: str,
        reason: str,
        target_id: int = 0,
        details: str = "",
        extra_lines: Optional[list] = None,
        color: int = Colors.PRIMARY,
        thumbnail_url: Optional[str] = None,
    ):
        super().__init__(timeout=None)

        body_lines = [f"{target_display}  —  by {actor_display}"]
        body_lines.append(f"**Reason:** {reason or 'No reason provided'}")
        if details:
            body_lines.append(f"**Details:** {details}")
        if extra_lines:
            body_lines.extend(extra_lines)

        ts = int(datetime.now(timezone.utc).timestamp())
        id_part = f"ID: {target_id}  •  " if target_id else ""
        footer = f"-# {BRAND_NAME}  •  {id_part}<t:{ts}:R>"

        section = ui.Section(
            ui.TextDisplay(f"# {emoji} {title}"),
            ui.TextDisplay("\n".join(body_lines)),
            ui.TextDisplay(footer),
            accessory=ui.Thumbnail(media=thumbnail_url or "https://cdn.discordapp.com/embed/avatars/0.png"),
        )

        container = ui.Container(section, accent_colour=color)
        self.add_item(container)
