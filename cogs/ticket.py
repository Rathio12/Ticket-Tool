"""Comprehensive, multi-guild ticket system cog.
Features:
- Panels (button or dropdown) with configurable options.
- Persistent JSON storage for panels, options, and per-guild config.
- Slash commands to create/manage panels and options.
- Automatic ticket channel creation with proper permissions.
- Message logging directly to a SQLite DB + JSON transcript backups.
- Inactivity auto-close timer.
- Star-rating DM after close.

Every server configures its own staff role(s), categories, and channels via
`/setup` — nothing here is hardcoded to a specific guild.
"""

import asyncio
import json
import datetime
import aiosqlite
from pathlib import Path
from typing import Optional, List, Dict

import discord
from discord.ext import commands, tasks
from discord import app_commands, ui

from core.config import OWNER_IDS, BOT_ERROR_CHANNEL_ID
from core.design import Colors, PanelView, EntryView, BRAND_NAME

class TicketDB:
    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.config_path = base_path / "config.json"
        self.db_path = base_path / "tickets.db"
        self.tickets_dir = base_path / "tickets"
        self.lock = asyncio.Lock()

        self.base_path.mkdir(parents=True, exist_ok=True)
        self.tickets_dir.mkdir(parents=True, exist_ok=True)

        if not self.config_path.exists():
            self._write_default_config()

    def _write_default_config(self):
        data = {
            "panels": {},
            "options": {},
            "server_config": {},
            "counters": {"panels": 0, "options": 0, "tickets": 0}
        }
        self.config_path.write_text(json.dumps(data, indent=4), encoding="utf-8")

    async def init_db(self):
        self._db = await aiosqlite.connect(self.db_path, timeout=30.0)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                panel_id INTEGER,
                option_id INTEGER,
                claimant_id INTEGER,
                created_at TEXT NOT NULL,
                closed_at TEXT,
                close_reason TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id);
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT DEFAULT '',
                attachments TEXT DEFAULT '[]',
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_ticket ON ticket_messages(ticket_id);
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                staff_id INTEGER,
                rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
                created_at TEXT NOT NULL,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_guild ON reviews(guild_id);
        """)
        await self._db.commit()
        await self._migrate_json_to_db()

    async def _migrate_json_to_db(self):
        """Import any JSON ticket files that aren't yet in the database."""
        if not self.tickets_dir.is_dir():
            return
        for guild_dir in self.tickets_dir.iterdir():
            if not guild_dir.is_dir():
                continue
            guild_id = int(guild_dir.name)
            for ticket_file in sorted(guild_dir.glob("*.json"), key=lambda p: int(p.stem)):
                try:
                    t = json.loads(ticket_file.read_text(encoding="utf-8"))
                    existing = await self.get_ticket(guild_id, t["id"])
                    if existing:
                        continue
                    await self._db.execute(
                        """INSERT OR IGNORE INTO tickets (id, channel_id, guild_id, creator_id, panel_id, option_id,
                           claimant_id, created_at, closed_at, close_reason, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (t["id"], t.get("channel_id", 0), guild_id,
                         t.get("creator_id", 0), t.get("panel_id"),
                         t.get("option_id"), t.get("claimant_id"),
                         t.get("created_at", ""), t.get("closed_at"),
                         t.get("close_reason", ""), t.get("status", "closed"))
                    )
                    for msg in t.get("messages", []):
                        await self._db.execute(
                            "INSERT INTO ticket_messages (ticket_id, timestamp, author_id, author_name, content, attachments) VALUES (?, ?, ?, ?, ?, ?)",
                            (t["id"], msg.get("timestamp", ""), msg.get("author_id", 0),
                             msg.get("author_name", ""), msg.get("content", ""),
                             json.dumps(msg.get("attachments", [])))
                        )
                    await self._db.commit()
                    print(f"[DB] Migrated ticket #{t['id']} from JSON (guild {guild_id})")
                except Exception as e:
                    print(f"[DB] Migration skipped ticket file {ticket_file.name}: {e}")

    def _read_config_sync(self):
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except:
            self._write_default_config()
            return json.loads(self.config_path.read_text(encoding="utf-8"))

    async def get_config(self):
        async with self.lock:
            return await asyncio.to_thread(self._read_config_sync)

    def _write_config_sync(self, data):
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        temp_path.replace(self.config_path)

    async def save_config(self, data):
        async with self.lock:
            try:
                await asyncio.to_thread(self._write_config_sync, data)
            except Exception as e:
                print(f"[DB] Error saving config: {e}")


    async def insert_ticket(self, ticket_data: dict):
        async with self.lock:
            await self._db.execute(
                """INSERT INTO tickets (id, channel_id, guild_id, creator_id, panel_id, option_id,
                   claimant_id, created_at, closed_at, close_reason, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticket_data["id"], ticket_data["channel_id"], ticket_data["guild_id"],
                 ticket_data["creator_id"], ticket_data.get("panel_id"),
                 ticket_data.get("option_id"), ticket_data.get("claimant_id"),
                 ticket_data["created_at"], ticket_data.get("closed_at"),
                 ticket_data.get("close_reason", ""), ticket_data["status"])
            )
            await self._db.commit()

    async def update_ticket(self, ticket_id: int, **kwargs):
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [ticket_id]
        async with self.lock:
            await self._db.execute(f"UPDATE tickets SET {sets} WHERE id = ?", vals)
            await self._db.commit()

    async def insert_message(self, ticket_id: int, msg: dict):
        async with self.lock:
            await self._db.execute(
                "INSERT INTO ticket_messages (ticket_id, timestamp, author_id, author_name, content, attachments) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, msg["timestamp"], msg["author_id"], msg["author_name"], msg["content"], json.dumps(msg.get("attachments", [])))
            )
            await self._db.commit()

    async def get_open_tickets(self, guild_id: int = None):
        async with self.lock:
            if guild_id:
                cursor = await self._db.execute("SELECT * FROM tickets WHERE guild_id = ? AND status = 'open'", (guild_id,))
            else:
                cursor = await self._db.execute("SELECT * FROM tickets WHERE status = 'open'")
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_closed_tickets(self, guild_id: int = None):
        async with self.lock:
            if guild_id:
                cursor = await self._db.execute("SELECT * FROM tickets WHERE guild_id = ? AND status = 'closed' ORDER BY closed_at DESC", (guild_id,))
            else:
                cursor = await self._db.execute("SELECT * FROM tickets WHERE status = 'closed' ORDER BY closed_at DESC")
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def find_ticket_by_channel(self, channel_id: int):
        async with self.lock:
            cursor = await self._db.execute("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_ticket(self, guild_id: int, ticket_id: int):
        async with self.lock:
            cursor = await self._db.execute("SELECT * FROM tickets WHERE id = ? AND guild_id = ?", (ticket_id, guild_id))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_last_message_time(self, ticket_id: int):
        async with self.lock:
            cursor = await self._db.execute("SELECT timestamp FROM ticket_messages WHERE ticket_id = ? ORDER BY id DESC LIMIT 1", (ticket_id,))
            row = await cursor.fetchone()
        return row["timestamp"] if row else None

    async def sync_counter(self):
        async with self.lock:
            cursor = await self._db.execute("SELECT MAX(id) as max_id FROM tickets")
            row = await cursor.fetchone()
            max_id = row["max_id"] if row and row["max_id"] else 0
        data = await self.get_config()
        if data["counters"]["tickets"] < max_id:
            data["counters"]["tickets"] = max_id
            await self.save_config(data)
            print(f"[DB] Synced ticket counter to {max_id}")


    async def insert_review(self, ticket_id: int, guild_id: int, staff_id: int, rating: int):
        async with self.lock:
            await self._db.execute(
                "INSERT INTO reviews (ticket_id, guild_id, staff_id, rating, created_at) VALUES (?, ?, ?, ?, ?)",
                (ticket_id, guild_id, staff_id, rating, datetime.datetime.now(datetime.timezone.utc).isoformat())
            )
            await self._db.commit()

    async def get_review_stats(self, guild_id: int) -> dict:
        async with self.lock:
            cursor = await self._db.execute(
                "SELECT COUNT(*) as total, AVG(rating) as avg FROM reviews WHERE guild_id = ?",
                (guild_id,)
            )
            row = await cursor.fetchone()
            cursor2 = await self._db.execute(
                "SELECT rating, COUNT(*) as count FROM reviews WHERE guild_id = ? GROUP BY rating ORDER BY rating",
                (guild_id,)
            )
            dist = {r: 0 for r in range(1, 6)}
            async for r in cursor2:
                dist[r["rating"]] = r["count"]
        return {"total": row["total"], "avg": round(row["avg"], 1) if row["avg"] else 0.0, "distribution": dist}

    async def get_recent_reviews(self, guild_id: int, limit: int = 5) -> list:
        async with self.lock:
            cursor = await self._db.execute(
                "SELECT ticket_id, staff_id, rating, created_at FROM reviews WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, limit)
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]


    def get_ticket_path(self, guild_id: int, ticket_id: int) -> Path:
        return self.tickets_dir / str(guild_id) / f"{ticket_id}.json"

    def _save_json_backup_sync(self, guild_id: int, ticket_data: dict):
        guild_dir = self.tickets_dir / str(guild_id)
        guild_dir.mkdir(parents=True, exist_ok=True)
        path = self.get_ticket_path(guild_id, ticket_data['id'])
        path.write_text(json.dumps(ticket_data, indent=4), encoding="utf-8")

    async def save_json_backup(self, guild_id: int, ticket_data: dict):
        await asyncio.to_thread(self._save_json_backup_sync, guild_id, ticket_data)


class TicketSelect(discord.ui.Select):
    def __init__(self, panel_id: int, options_data: List[dict]):
        self.panel_id = panel_id
        discord_options = []
        for opt in options_data:
            discord_options.append(
                discord.SelectOption(
                    label=opt["label"],
                    emoji=opt.get("emoji"),
                    description=opt.get("description"),
                    value=str(opt["id"])
                )
            )

        super().__init__(
            placeholder="Select a ticket category...",
            min_values=1,
            max_values=1,
            options=discord_options,
            custom_id=f"ticket_panel_{panel_id}"
        )

class TicketPanelView(discord.ui.View):
    def __init__(self, panel_id: int, options_data: List[dict]):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(panel_id, options_data))


class TicketPanelLayout(ui.LayoutView):
    """V2 panel message: info header + interactive dropdown/buttons in one LayoutView."""
    def __init__(self, panel_id: int, options_data: List[dict], *,
                 panel_name: str = "Support Tickets",
                 custom_text: str = "",
                 thumb: str = None,
                 panel_type: str = "dropdown",
                 callback_factory=None):
        super().__init__(timeout=None)
        desc = (
            (f"{custom_text}\n\n" if custom_text else "")
            + "### How to get help:\n"
            "> **1.** Choose the category that best fits your issue.\n"
            "> **2.** A private channel will be created just for you.\n"
            "> **3.** Describe your issue — a staff member will respond shortly.\n\n"
            "-# Response times may vary. Please be patient."
        )
        header = ui.TextDisplay(f"# 🎫 {panel_name}")
        body = ui.TextDisplay(desc)
        footer = ui.TextDisplay(f"-# {BRAND_NAME}  •  Click below to open a ticket")
        if thumb:
            container = ui.Container(
                ui.Section(header, body, footer, accessory=ui.Thumbnail(media=thumb)),
                accent_colour=Colors.PRIMARY,
            )
        else:
            container = ui.Container(header, body, footer, accent_colour=Colors.PRIMARY)
        self.add_item(container)
        if panel_type == "dropdown":
            self.add_item(ui.ActionRow(TicketSelect(panel_id, options_data)))
        else:
            btns = []
            for opt in options_data:
                b = ui.Button(label=opt["label"], emoji=opt.get("emoji"),
                              style=discord.ButtonStyle.primary,
                              custom_id=f"ticket_btn_{panel_id}_{opt['id']}")
                if callback_factory:
                    b.callback = callback_factory(panel_id, opt["id"])
                btns.append(b)
            for chunk_start in range(0, len(btns), 5):
                self.add_item(ui.ActionRow(*btns[chunk_start:chunk_start + 5]))


CLOSE_REASON_PRESETS = [
    "Issue resolved.",
    "No response from user.",
    "Duplicate ticket.",
    "User requested closure.",
    "Handled by staff.",
    "Abuse / False report.",
    "Custom..."
]

class TicketCloseReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=r, value=r, emoji="🔒")
            for r in CLOSE_REASON_PRESETS
        ]
        super().__init__(placeholder="Select a closing reason...", min_values=1, max_values=1, options=options, custom_id="close_reason_select")

    async def callback(self, interaction: discord.Interaction):
        reason = self.values[0]
        if reason == "Custom...":
            await interaction.response.send_modal(TicketCloseCustomModal())
        else:
            cog = interaction.client.get_cog("TicketCog")
            if cog:
                await cog.close_ticket_process(interaction, reason)

class TicketCloseReasonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCloseReasonSelect())

class TicketCloseCustomModal(discord.ui.Modal, title='Custom Close Reason'):
    reason = discord.ui.TextInput(
        label='Closing Reason',
        style=discord.TextStyle.paragraph,
        placeholder='Enter a custom reason for closing this ticket.',
        required=True,
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.close_ticket_process(interaction, self.reason.value.strip())

class TicketTransferRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Select a department role...", min_values=1, max_values=1, custom_id="transfer_role_select")

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.transfer_ticket_process(interaction, self.values[0])

class TicketTransferView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTransferRoleSelect())

class TicketRatingView(ui.LayoutView):
    def __init__(self, ticket_id: str = "0", staff_id: int = None, guild_id: int = 0,
                 *, guild_name: str = "", guild_icon: str = None):
        super().__init__(timeout=86400)
        self.ticket_id = ticket_id
        self.staff_id = staff_id
        self.guild_id = guild_id

        btns = []
        for i in range(1, 6):
            b = ui.Button(label=f"{i} ⭐", style=discord.ButtonStyle.secondary, custom_id=f"rate_{i}")
            b.callback = self._make_rate_cb(i)
            btns.append(b)

        info_text = (
            f"Thank you for reaching out to **{guild_name or 'our support'}"
            "** team!\nYour feedback helps us improve. Please rate the service you received.\n\n"
            f"**🎫 Ticket:** `#{ticket_id}`    **👤 Staff:** "
            f"{f'<@{staff_id}>' if staff_id else 'Auto-assigned'}"
        )
        td_title = ui.TextDisplay("# ⭐ How was your experience?")
        td_body  = ui.TextDisplay(info_text)
        if guild_icon:
            section = ui.Section(td_title, td_body, accessory=ui.Thumbnail(media=guild_icon))
        else:
            section = ui.Section(td_title, td_body)
        container = ui.Container(
            section,
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(f"-# {BRAND_NAME}  •  Tap a star to rate  •  Expires in 24h"),
            accent_colour=Colors.GOLD,
        )
        self.add_item(container)
        self.add_item(ui.ActionRow(*btns))

    def _make_rate_cb(self, rating: int):
        async def cb(interaction: discord.Interaction):
            await self.handle_rating(interaction, rating)
        return cb

    async def handle_rating(self, interaction: discord.Interaction, rating: int):
        await interaction.response.send_message(
            f"✅ Thank you! You rated your support experience **{rating} Stars**.", ephemeral=True)
        fresh = discord.ui.LayoutView.from_message(interaction.message, timeout=self.timeout)
        for item in fresh.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.message.edit(view=fresh)

        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.db.insert_review(int(self.ticket_id), self.guild_id, self.staff_id, rating)
            guild = cog.bot.get_guild(self.guild_id)
            if not guild:
                return
            stars = "⭐" * rating + "☆" * (5 - rating)
            staff_mention = f"<@{self.staff_id}>" if self.staff_id else "Auto-assigned"
            guild_icon = guild.icon.url if guild.icon else None

            data = await cog.db.get_config()
            cfg = data.get("server_config", {}).get(str(self.guild_id), {})

            rc_id = cfg.get("review_channel_id")
            rc = guild.get_channel(rc_id) if rc_id else None
            if rc:
                try:
                    await rc.send(
                        view=EntryView(
                            emoji="⭐",
                            title="Support Feedback Received",
                            target_display=f"Ticket `#{self.ticket_id}`",
                            actor_display=interaction.user.mention,
                            reason=f"{stars} ({rating}/5)",
                            extra_lines=[f"**Staff:** {staff_mention}"],
                            color=Colors.GOLD,
                            thumbnail_url=guild_icon,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except:
                    pass

            tc_id = cfg.get("transcript_channel_id")
            if tc_id:
                tc = guild.get_channel(tc_id)
                if tc:
                    await cog.safe_send(tc,
                        view=PanelView(
                            title="⭐ New Ticket Rating",
                            description=(
                                f"Ticket **#{self.ticket_id}** rated **{rating}/5** "
                                f"by {interaction.user.mention}\n**Staff:** {staff_mention}"
                            ),
                            color=Colors.GOLD,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
class TicketOpenLayout(ui.LayoutView):
    def __init__(self, *, ticket_id: int = 0, label: str = "", emoji: str = "🎫",
                 creator_mention: str = "", icon_url: str = "", opened_ts: str = ""):
        super().__init__(timeout=None)

        claim_btn = ui.Button(label="Claim", style=discord.ButtonStyle.success, emoji="👋", custom_id="claim_ticket_btn")
        claim_btn.callback = self._claim
        escalate_btn = ui.Button(label="Escalate", style=discord.ButtonStyle.primary, emoji="⬆️", custom_id="escalate_ticket_btn")
        escalate_btn.callback = self._escalate
        transfer_btn = ui.Button(label="Transfer", style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="transfer_ticket_btn")
        transfer_btn.callback = self._transfer
        close_btn = ui.Button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket_btn")
        close_btn.callback = self._close

        container = ui.Container(
            ui.Section(
                ui.TextDisplay(f"# {emoji} {label} Ticket" if label else "# 🎫 Ticket"),
                ui.TextDisplay(
                    f"Welcome {creator_mention}! Describe your issue and a staff member will assist you shortly.\n"
                    f"-# Keep all communication in this channel."
                ),
                accessory=ui.Thumbnail(media=icon_url or "https://cdn.discordapp.com/embed/avatars/0.png"),
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                f"**📋 Status:** `Open — Awaiting Staff`    **🆔 Ticket:** `#{ticket_id}`\n"
                f"**⏰ Opened:** {opened_ts}\n"
                f"-# 🎟️ ticket_id:{ticket_id}"
            ),
            accent_colour=Colors.PRIMARY,
        )
        row = ui.ActionRow(claim_btn, escalate_btn, transfer_btn, close_btn)
        self.add_item(container)
        self.add_item(row)

    async def _claim(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog: await cog.claim_ticket_process(interaction)

    async def _escalate(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog: await cog.escalate_ticket_process(interaction)

    async def _transfer(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog and not cog.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        await interaction.response.send_message("Select a role to transfer this ticket to:", view=TicketTransferView(), ephemeral=True)

    async def _close(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            ticket = cog.ticket_cache.get(interaction.channel.id)
            is_creator = ticket and ticket.get("creator_id") == interaction.user.id
            if not is_creator and not cog.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ This command is restricted to the ticket creator, staff, or owners.", ephemeral=True)
        await interaction.response.send_message("Select a reason for closing this ticket:", view=TicketCloseReasonView(), ephemeral=True)


class TicketClosedLayout(ui.LayoutView):
    def __init__(self, *, ticket_id: int = 0, creator_id: int = 0, reason: str = ""):
        super().__init__(timeout=None)

        transcript_btn = ui.Button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📄", custom_id="transcript_btn")
        transcript_btn.callback = self._transcript
        reopen_btn = ui.Button(label="Reopen", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="reopen_btn")
        reopen_btn.callback = self._reopen
        delete_btn = ui.Button(label="Delete", style=discord.ButtonStyle.danger, emoji="⛔", custom_id="delete_btn")
        delete_btn.callback = self._delete

        container = ui.Container(
            ui.TextDisplay("# 🔒 Ticket Closed"),
            ui.TextDisplay(
                f"**Reason:** {reason or 'No specific reason provided.'}\n\n"
                f"**🆔 Ticket:** `#{ticket_id}`    **👤 Creator:** <@{creator_id}>\n"
                f"-# Staff may still manage this ticket using the controls below."
            ),
            accent_colour=Colors.ERROR,
        )
        row = ui.ActionRow(transcript_btn, reopen_btn, delete_btn)
        self.add_item(container)
        self.add_item(row)

    async def _transcript(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog: await cog.generate_transcript_action(interaction)

    async def _reopen(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("TicketCog")
        if cog: await cog.reopen_ticket_process(interaction)

    async def _delete(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cog = interaction.client.get_cog("TicketCog")
        if cog: await cog.process_ticket_deletion(interaction)
        else: await interaction.channel.delete()


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, emoji="📄", custom_id="transcript_btn")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.generate_transcript_action(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="user_close_btn")
    async def user_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if not cog:
            return
        ticket = cog.ticket_cache.get(interaction.channel.id)
        if not ticket or ticket.get("status") != "open":
            return await interaction.response.send_message("❌ This ticket is not open.", ephemeral=True)
        is_creator = ticket.get("creator_id") == interaction.user.id
        is_staff = isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.administrator or
            interaction.user.guild_permissions.manage_channels
        )
        if not is_creator and not is_staff:
            return await interaction.response.send_message("❌ Only the ticket creator or staff can close this ticket.", ephemeral=True)
        await interaction.response.send_message("Select a reason for closing:", view=TicketCloseReasonView(), ephemeral=True)

    @discord.ui.button(label="Open", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="reopen_btn")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.reopen_ticket_process(interaction)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, emoji="⛔", custom_id="delete_btn")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.process_ticket_deletion(interaction)
        else:
            await interaction.channel.delete()

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="👋", custom_id="claim_ticket_btn")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.claim_ticket_process(interaction)

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.primary, emoji="⬆️", custom_id="escalate_ticket_btn")
    async def escalate_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            await cog.escalate_ticket_process(interaction)

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="transfer_ticket_btn")
    async def transfer_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog and not cog.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        await interaction.response.send_message("Select a role to transfer this ticket to:", view=TicketTransferView(), ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketCog")
        if cog:
            ticket = cog.ticket_cache.get(interaction.channel.id)
            is_creator = ticket and ticket.get("creator_id") == interaction.user.id
            if not is_creator and not cog.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ This command is restricted to the ticket creator, staff, or owners.", ephemeral=True)
        await interaction.response.send_message("Select a reason for closing this ticket:", view=TicketCloseReasonView(), ephemeral=True)


class TicketCog(commands.Cog, name="TicketCog"):
    ticket = app_commands.Group(name="ticket", description="Ticket management commands")
    option = app_commands.Group(name="option", description="Manage ticket options", parent=ticket)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.base_data_path = Path(__file__).parents[1] / "Data"
        self.db = TicketDB(self.base_data_path)
        self.bot.loop.create_task(self._init())

        self.server_configs: Dict[str, dict] = {}

        self.auto_permission_repair.start()
        self.backup_loop.start()
        self.ticket_cache: Dict[int, dict] = {}
        self._dirty_cache: set[int] = set()

    async def _init(self):
        await self.bot.wait_until_ready()
        self.bot.ui.append_log("", "INFO", "ticket: initialising database…")
        await self.db.init_db()
        await self.db.sync_counter()
        data = await self.db.get_config()
        self.server_configs = data.get("server_config", {})
        self.bot.add_view(TicketCloseView())
        self.bot.add_view(TicketControlsView())
        self.bot.add_view(TicketOpenLayout())
        self.bot.add_view(TicketClosedLayout())
        self.bot.add_view(TicketTransferView())
        self.bot.add_view(TicketCloseReasonView())
        await self.restore_persistent_views()
        self.bot.ui.append_log("", "OK", "ticket: ready — views restored")
        await self.initialize_cache()

    async def _save_config(self, data: dict):
        """Persist config and keep the in-memory server_config mirror in sync."""
        await self.db.save_config(data)
        self.server_configs = data.get("server_config", {})

    def _cfg(self, guild_id: int) -> dict:
        return self.server_configs.get(str(guild_id), {})

    async def initialize_cache(self):
        """Initializes the ticket cache from the database with last message times.
        Caches open + closed tickets whose channels still exist (for reopen)."""
        print("[TICKETS] Initializing ticket cache from database...")
        for t in await self.db.get_open_tickets():
            last_ts = await self.db.get_last_message_time(t["id"])
            if last_ts:
                t["messages"] = [{"timestamp": last_ts}]
            self.ticket_cache[t["channel_id"]] = t
        for t in await self.db.get_closed_tickets():
            chan = self.bot.get_channel(t["channel_id"])
            if chan:
                self.ticket_cache[t["channel_id"]] = t
        print(f"[TICKETS] Cache initialized — {len(self.ticket_cache)} tickets tracked.")
        self.inactivity_check.start()
        self.sla_check.start()

    def _mark_dirty(self, channel_id: int):
        """Flag a ticket channel to be backed up on the next loop tick."""
        self._dirty_cache.add(channel_id)

    @tasks.loop(seconds=30)
    async def backup_loop(self):
        """Every 30 seconds, flush all dirty tickets to JSON backup files."""
        if not self._dirty_cache:
            return
        batch = list(self._dirty_cache)
        self._dirty_cache.clear()
        for ch_id in batch:
            ticket = self.ticket_cache.get(ch_id)
            if ticket:
                try:
                    await self.db.save_json_backup(ticket["guild_id"], ticket)
                except:
                    pass

    @backup_loop.before_loop
    async def before_backup(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def inactivity_check(self):
        """Auto-close tickets with no activity for the configured timeout."""
        for ch_id, ticket in list(self.ticket_cache.items()):
            if ticket.get("status") != "open":
                continue
            cfg = self._cfg(ticket["guild_id"])
            timeout_hours = cfg.get("inactivity_hours", 48)
            if not timeout_hours:
                continue
            last_ts = await self.db.get_last_message_time(ticket["id"])
            if not last_ts:
                last_ts = ticket["created_at"]
            try:
                last_dt = datetime.datetime.fromisoformat(last_ts)
                now = datetime.datetime.now(datetime.timezone.utc)
                if (now - last_dt).total_seconds() > timeout_hours * 3600:
                    channel = self.bot.get_channel(ch_id)
                    if channel:
                        reason = f"Auto-closed due to {timeout_hours}h of inactivity."
                        await self._run_close_flow(channel, ticket, reason=reason)
            except:
                pass

    @inactivity_check.before_loop
    async def before_inactivity(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=2)
    async def sla_check(self):
        """First-response SLA: if no staff member has replied within the
        configured window, ping the staff/ping role once as a reminder.
        This is the bot's flagship differentiator — most ticket bots only
        track close time, not whether anyone has actually responded yet."""
        for ch_id, ticket in list(self.ticket_cache.items()):
            if ticket.get("status") != "open" or ticket.get("first_response_at"):
                continue
            cfg = self._cfg(ticket["guild_id"])
            sla_minutes = cfg.get("sla_minutes", 15)
            if not sla_minutes or ticket.get("_sla_reminded"):
                continue
            try:
                created_dt = datetime.datetime.fromisoformat(ticket["created_at"])
                elapsed_min = (datetime.datetime.now(datetime.timezone.utc) - created_dt).total_seconds() / 60
                if elapsed_min < sla_minutes:
                    continue
                channel = self.bot.get_channel(ch_id)
                if not channel:
                    continue
                ticket["_sla_reminded"] = True
                ping_role_id = cfg.get("staff_role_id") or cfg.get("ping_role_id")
                mention = f"<@&{ping_role_id}>" if ping_role_id else ""
                if mention:
                    await channel.send(content=mention, allowed_mentions=discord.AllowedMentions(roles=True))
                await channel.send(view=PanelView(
                    title="⏱️ SLA Reminder",
                    description=(
                        f"This ticket has had **no staff response** for over `{sla_minutes}` minutes.\n"
                        f"Please take a look when you get a chance."
                    ),
                    color=Colors.WARNING,
                ))
                esc_chan_id = cfg.get("escalation_channel_id")
                if esc_chan_id:
                    esc_chan = self.bot.get_channel(esc_chan_id)
                    await self.safe_send(esc_chan, view=PanelView(
                        title="⏱️ SLA Breach",
                        description=f"Ticket #{ticket['id']} ({channel.mention}) has gone `{sla_minutes}m+` without a staff reply.",
                        color=Colors.WARNING,
                    ))
            except Exception:
                pass

    @sla_check.before_loop
    async def before_sla_check(self):
        await self.bot.wait_until_ready()

    def is_staff_or_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in OWNER_IDS:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        cfg = self._cfg(interaction.guild.id)
        staff_ids = {cfg.get("staff_role_id"), cfg.get("staff2_role_id"), cfg.get("admin_role_id")}
        staff_ids.discard(None)
        staff_ids.discard(interaction.guild.id)
        user_role_ids = {r.id for r in interaction.user.roles}
        return bool(staff_ids & user_role_ids)

    async def report_error(self, context: str, error: Exception, interaction: Optional[discord.Interaction] = None):
        """Reports errors to the guild's configured error channel, falling back
        to the bot operator's global error channel if none is configured."""
        import traceback
        tb = traceback.format_exc()
        print(f"Error in {context}: {error}\n{tb}")

        channel_id = None
        if interaction and interaction.guild:
            channel_id = self._cfg(interaction.guild.id).get("error_channel_id")
        if not channel_id:
            channel_id = BOT_ERROR_CHANNEL_ID
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except:
                return

        error_fields = [
            ("Context",     context,                                              False),
            ("Error",       f"`{type(error).__name__}: {str(error)}`",            False),
            ("Stack Trace", f"```py\n{tb}\n```",                                 False),
        ]
        if interaction:
            error_fields.append(("Guild",   f"{interaction.guild.name} (`{interaction.guild.id}`)", True))
            error_fields.append(("User",    f"{interaction.user} (`{interaction.user.id}`)",         True))
            if interaction.channel:
                error_fields.append(("Channel", f"#{interaction.channel.name} (`{interaction.channel.id}`)", True))
        try:
            await channel.send(view=PanelView(
                title="🚨 Internal Error Reported",
                fields=error_fields,
                color=Colors.ERROR,
                timestamp=True,
            ))
        except Exception as send_error:
            print(f"Failed to send error report to notify-channel: {send_error}")

    async def _grant_access(self, channel, member: discord.abc.Snowflake):
        """Give a member access to a ticket, whether it's a channel or a thread."""
        if member is None:
            return
        try:
            if isinstance(channel, discord.Thread):
                await channel.add_user(member)
            else:
                await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass

    async def _revoke_access(self, channel, member: discord.abc.Snowflake):
        """Remove a member's access to a ticket, whether it's a channel or a thread."""
        if member is None:
            return
        try:
            if isinstance(channel, discord.Thread):
                await channel.remove_user(member)
            else:
                await channel.set_permissions(member, overwrite=None)
        except Exception:
            pass

    async def _grant_role_access(self, channel, role: Optional[discord.Role]):
        if role is None:
            return
        try:
            if isinstance(channel, discord.Thread):
                parent = channel.parent
                if parent:
                    await parent.set_permissions(role, view_channel=True, manage_threads=True, send_messages_in_threads=True, read_message_history=True)
            else:
                await channel.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass

    async def _find_repeat_ticket(self, guild_id: int, creator_id: int, option_id: int, hours: int = 72) -> Optional[dict]:
        if not hours:
            return None
        closed = await self.db.get_closed_tickets(guild_id)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        best = None
        for t in closed:
            if t.get("creator_id") != creator_id or t.get("option_id") != option_id:
                continue
            closed_at = t.get("closed_at")
            if not closed_at:
                continue
            try:
                dt = datetime.datetime.fromisoformat(closed_at)
            except Exception:
                continue
            if dt < cutoff:
                continue
            if not best or dt > best[0]:
                best = (dt, t)
        return best[1] if best else None

    async def safe_send(self, channel, **kwargs):
        """Sends a message to a channel, attempting a repair if permissions are missing."""
        if not channel: return None
        try:
            return await channel.send(**kwargs)
        except discord.Forbidden:
            if hasattr(channel, "guild") and channel.guild:
                await self.auto_repair_guild_permissions(channel.guild)
                try:
                    return await channel.send(**kwargs)
                except: pass
        except: pass
        return None

    async def restore_persistent_views(self):
        await self.bot.wait_until_ready()
        data = await self.db.get_config()
        for pid, panel in data.get("panels", {}).items():
            options = [opt for opt in data.get("options", {}).values() if str(opt["panel_id"]) == str(pid)]

            if panel["type"] == "dropdown":
                self.bot.add_view(TicketPanelView(int(pid), options))
                self.bot.add_view(TicketPanelLayout(int(pid), options))
            elif panel["type"] == "button":
                self.bot.add_view(TicketPanelLayout(
                    int(pid), options, panel_type="button",
                    callback_factory=self._make_button_callback,
                ))

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")

            if interaction.response.is_done():
                return

            if custom_id.startswith("ticket_panel_"):
                if not interaction.data.get("values"):
                    return
                panel_id = int(custom_id.split("_")[2])
                option_id = int(interaction.data["values"][0])

                await interaction.response.defer(ephemeral=True)

                data = await self.db.get_config()
                options = [opt for opt in data.get("options", {}).values() if str(opt["panel_id"]) == str(panel_id)]
                if options:
                    try:
                        fresh = discord.ui.LayoutView.from_message(interaction.message, timeout=None)
                        await interaction.message.edit(view=fresh)
                    except Exception:
                        pass

                await self.create_ticket(interaction, panel_id, option_id)

    @commands.Cog.listener()
    async def on_ready(self):
        """Run a permission repair for all guilds at startup."""
        print(f"[TICKETS] Bot ready, running permission audit for {len(self.bot.guilds)} guild(s)...")
        for guild in self.bot.guilds:
            await self.auto_repair_guild_permissions(guild)

    @tasks.loop(hours=6)
    async def auto_permission_repair(self):
        """Periodically ensures the bot has access to all configured channels."""
        for guild in self.bot.guilds:
            await self.auto_repair_guild_permissions(guild)

    @auto_permission_repair.before_loop
    async def before_repair(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="setup", description="One-command setup: panel, categories, roles, and channels for your ticket system.")
    @app_commands.describe(
        panel_name="Name of your ticket panel (e.g. Support)",
        custom_text="Description text on the panel",
        ticket_mode="Open tickets as separate channels, or as private threads in one channel",
        staff_role="Role that gets access to every ticket and can claim/manage them",
        ping_role="Role to ping when a ticket opens (defaults to staff_role)",
        category="[Channel mode] Category where OPEN tickets will go",
        thread_channel="[Thread mode] Channel private ticket threads are created in",
        panel_type="dropdown or button (default: dropdown)",
        template="Choose a pre-defined template for your first option",
        admin_role="Higher-up role used for escalation",
        staff_role_2="Optional second staff/support role with ticket access",
        escalation_channel="Optional channel to send escalation logs",
        review_channel="Optional channel to log star ratings",
        error_channel="Optional channel for internal error reports",
        thumbnail_url="Custom image URL for the panel thumbnail",
        transcript_channel="Optional existing channel for transcripts",
        closed_category="[Channel mode] Optional existing category for closed tickets"
    )
    @app_commands.choices(
        panel_type=[
            app_commands.Choice(name="Dropdown Menu", value="dropdown"),
            app_commands.Choice(name="Buttons", value="button")
        ],
        ticket_mode=[
            app_commands.Choice(name="Channels (a separate channel per ticket)", value="channel"),
            app_commands.Choice(name="Threads (a private thread per ticket, in one channel)", value="thread"),
        ],
        template=[
            app_commands.Choice(name="General Support",          value="General Support"),
            app_commands.Choice(name="Billing & Payments",       value="Billing"),
            app_commands.Choice(name="Report a User/Bug",        value="Report"),
            app_commands.Choice(name="Partnership",              value="Partnership"),
            app_commands.Choice(name="Technical Support",        value="Technical"),
            app_commands.Choice(name="All Departments (All 5)",  value="All Departments")
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_mega(
        self,
        interaction: discord.Interaction,
        panel_name: str = "Support Tickets",
        custom_text: str = "Select an option below to open a ticket.",
        ticket_mode: app_commands.Choice[str] = None,
        staff_role: discord.Role = None,
        ping_role: discord.Role = None,
        category: discord.CategoryChannel = None,
        thread_channel: discord.TextChannel = None,
        panel_type: app_commands.Choice[str] = None,
        template: app_commands.Choice[str] = None,
        admin_role: discord.Role = None,
        staff_role_2: discord.Role = None,
        escalation_channel: discord.TextChannel = None,
        review_channel: discord.TextChannel = None,
        error_channel: discord.TextChannel = None,
        thumbnail_url: str = None,
        transcript_channel: discord.TextChannel = None,
        closed_category: discord.CategoryChannel = None
    ):
        deferred = False
        try:
            await interaction.response.defer(ephemeral=True)
            deferred = True
            guild = interaction.guild

            for role_param, role_val in (("staff_role", staff_role), ("staff_role_2", staff_role_2), ("admin_role", admin_role), ("ping_role", ping_role)):
                if role_val is not None and role_val.id == guild.id:
                    return await interaction.followup.send(f"❌ `{role_param}` can't be @everyone — pick a real staff role.", ephemeral=True)

            perms = guild.me.guild_permissions
            if not perms.manage_channels or not perms.manage_roles:
                missing = []
                if not perms.manage_channels: missing.append("`Manage Channels`")
                if not perms.manage_roles: missing.append("`Manage Roles`")
                return await interaction.followup.send(f"❌ I am missing the following required permissions to complete setup: {', '.join(missing)}. Please grant these and try again.", ephemeral=True)

            data = await self.db.get_config()

            if str(guild.id) not in data["server_config"]:
                data["server_config"][str(guild.id)] = {}
            cfg = data["server_config"][str(guild.id)]

            mode = ticket_mode.value if ticket_mode else cfg.get("ticket_mode", "channel")
            if mode not in ("channel", "thread"):
                mode = "channel"
            cfg["ticket_mode"] = mode

            if staff_role:
                cfg["staff_role_id"] = staff_role.id
            if staff_role_2:
                cfg["staff2_role_id"] = staff_role_2.id

            ping_role = ping_role or staff_role
            if ping_role:
                cfg["ping_role_id"] = ping_role.id

            if admin_role:
                cfg["admin_role_id"] = admin_role.id

            if review_channel:
                cfg["review_channel_id"] = review_channel.id

            if error_channel:
                cfg["error_channel_id"] = error_channel.id

            if escalation_channel:
                try:
                    await escalation_channel.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
                except:
                    pass
                cfg["escalation_channel_id"] = escalation_channel.id

            if transcript_channel:
                try:
                    await transcript_channel.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
                except:
                    pass
                cfg["transcript_channel_id"] = transcript_channel.id
            else:
                existing_id = cfg.get("transcript_channel_id")
                if not existing_id or not guild.get_channel(existing_id):
                    try:
                        tc = await guild.create_text_channel(
                            "📜-transcripts",
                            overwrites={
                                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
                            }
                        )
                        cfg["transcript_channel_id"] = tc.id
                    except discord.Forbidden:
                        return await interaction.followup.send("❌ I do not have permission to create the transcript channel. Please ensure I have 'Manage Channels'.", ephemeral=True)

            if mode == "channel":
                if closed_category:
                    try:
                        await closed_category.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                    except:
                        pass
                    cfg["closed_category_id"] = closed_category.id
                else:
                    existing_id = cfg.get("closed_category_id")
                    if not existing_id or not guild.get_channel(existing_id):
                        overwrites_closed = {
                            guild.default_role: discord.PermissionOverwrite(read_messages=False),
                            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                        }
                        if ping_role:
                            overwrites_closed[ping_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                        try:
                            cc = await guild.create_category("🔒 CLOSED TICKETS", overwrites=overwrites_closed)
                            cfg["closed_category_id"] = cc.id
                        except discord.Forbidden:
                            return await interaction.followup.send("❌ I do not have permission to create the 'Closed Tickets' category.", ephemeral=True)

                if not category:
                    overwrites_cat = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                    }
                    if ping_role:
                        overwrites_cat[ping_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

                    try:
                        category = await guild.create_category(f"🎫 {panel_name}", overwrites=overwrites_cat)
                    except discord.Forbidden:
                        return await interaction.followup.send("❌ I do not have permission to create the ticket category.", ephemeral=True)
                else:
                    try:
                        await category.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                    except:
                        pass
            else:
                staff_ids_for_perms = [r for r in (cfg.get("staff_role_id"), cfg.get("staff2_role_id"), cfg.get("admin_role_id")) if r]
                if thread_channel:
                    parent = thread_channel
                else:
                    existing_id = cfg.get("thread_channel_id")
                    parent = guild.get_channel(existing_id) if existing_id else None
                    if not parent:
                        parent = discord.utils.find(
                            lambda c: "ticket" in c.name.lower(),
                            guild.text_channels,
                        )
                    if not parent:
                        try:
                            parent = await guild.create_text_channel(f"🎫-{panel_name.lower().replace(' ', '-')}")
                        except discord.Forbidden:
                            return await interaction.followup.send("❌ I do not have permission to create the ticket thread channel.", ephemeral=True)

                cfg["thread_channel_id"] = parent.id
                try:
                    overwrites_thread = {
                        guild.default_role: discord.PermissionOverwrite(
                            view_channel=True, send_messages=False,
                            create_private_threads=False, create_public_threads=False,
                        ),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True,
                            manage_channels=True, manage_threads=True, create_private_threads=True,
                            send_messages_in_threads=True,
                        ),
                    }
                    for role_id in staff_ids_for_perms:
                        role = guild.get_role(role_id)
                        if role:
                            overwrites_thread[role] = discord.PermissionOverwrite(
                                view_channel=True, manage_threads=True, send_messages_in_threads=True,
                                read_message_history=True,
                            )
                    await parent.edit(overwrites=overwrites_thread)
                except Exception:
                    pass

            data["counters"]["panels"] += 1
            panel_id = str(data["counters"]["panels"])

            p_type = panel_type.value if panel_type else "dropdown"
            if p_type not in ["dropdown", "button"]:
                p_type = "dropdown"

            data["panels"][panel_id] = {
                "id": int(panel_id),
                "name": panel_name,
                "category_id": category.id if mode == "channel" else None,
                "channel_id": interaction.channel.id,
                "type": p_type,
                "message_id": None
            }

            t_label = template.value if template else "General Support"
            options_to_add = []

            if t_label == "All Departments":
                options_to_add = [
                    {"label": "General Support",     "emoji": "📩", "desc": "Open a general support ticket"},
                    {"label": "Billing Support",      "emoji": "💳", "desc": "Issues with payments or subscriptions"},
                    {"label": "Report User/Bug",      "emoji": "⚠️", "desc": "Report rule breakers or system bugs"},
                    {"label": "Partnership",          "emoji": "🤝", "desc": "Discuss partnership opportunities"},
                    {"label": "Technical Support",    "emoji": "🛠️", "desc": "Get help with a technical issue"},
                ]
            elif t_label == "Billing":
                options_to_add = [{"label": "Billing Support",   "emoji": "💳", "desc": "Issues with payments or subscriptions"}]
            elif t_label == "Report":
                options_to_add = [{"label": "Report User/Bug",   "emoji": "⚠️", "desc": "Report rule breakers or system bugs"}]
            elif t_label == "Partnership":
                options_to_add = [{"label": "Partnership",       "emoji": "🤝", "desc": "Discuss partnership opportunities"}]
            elif t_label == "Technical":
                options_to_add = [{"label": "Technical Support", "emoji": "🛠️", "desc": "Get help with a technical issue"}]
            else:
                options_to_add = [{"label": "General Support",   "emoji": "📩", "desc": "Open a general support ticket"}]

            added_options = []
            for opt in options_to_add:
                data["counters"]["options"] += 1
                opt_id = str(data["counters"]["options"])
                opt_data = {
                    "id": int(opt_id),
                    "panel_id": int(panel_id),
                    "label": opt["label"],
                    "emoji": opt["emoji"],
                    "description": opt["desc"]
                }
                data["options"][opt_id] = opt_data
                added_options.append(opt_data)

            await self._save_config(data)

            thumb = thumbnail_url or (interaction.guild.icon.url if interaction.guild.icon else None)
            if p_type == "dropdown":
                panel_layout = TicketPanelLayout(
                    int(panel_id), added_options,
                    panel_name=panel_name, custom_text=custom_text or "", thumb=thumb,
                    panel_type="dropdown",
                )
            else:
                panel_layout = TicketPanelLayout(
                    int(panel_id), added_options,
                    panel_name=panel_name, custom_text=custom_text or "", thumb=thumb,
                    panel_type="button", callback_factory=self._make_button_callback,
                )
            message = await interaction.channel.send(view=panel_layout)

            data = await self.db.get_config()
            data["panels"][panel_id]["message_id"] = message.id
            await self._save_config(data)

            if not cfg.get("staff_role_id"):
                await interaction.followup.send(
                    f"✅ Setup complete! Panel sent to {interaction.channel.mention}.\n"
                    f"⚠️ No `staff_role` was set — only server Administrators can manage tickets right now. "
                    f"Run `/setup` again with `staff_role:` to let your support team claim/close tickets.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(f"✅ Setup complete! Everything is configured and the panel was sent to {interaction.channel.mention}", ephemeral=True)
        except Exception as e:
            await self.report_error("Mega Setup", e, interaction)
            if deferred:
                await interaction.followup.send(f"❌ Setup failed: {e}", ephemeral=True)

    async def refresh_panel_message(self, guild: discord.Guild, panel_id: int):
        data = await self.db.get_config()

        panel = data["panels"].get(str(panel_id))
        if not panel or not panel.get("message_id"):
            return

        options = [opt for opt in data["options"].values() if str(opt["panel_id"]) == str(panel_id)]

        channel_id = panel.get("channel_id")
        message = None

        if channel_id:
            try:
                channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
                if channel:
                    message = await channel.fetch_message(panel["message_id"])
            except:
                pass

        if not message:
            for ch in guild.text_channels:
                try:
                    message = await ch.fetch_message(panel["message_id"])
                    if message:
                        panel["channel_id"] = ch.id
                        await self._save_config(data)
                        break
                except:
                    continue

        if not message:
            return

        if panel["type"] == "dropdown":
            view = TicketPanelView(panel_id, options)
            await message.edit(view=view)
        else:
            view = discord.ui.View(timeout=None)
            for opt in options:
                btn = discord.ui.Button(label=opt["label"], emoji=opt.get("emoji"), style=discord.ButtonStyle.primary, custom_id=f"ticket_btn_{panel_id}_{opt['id']}")
                btn.callback = self._make_button_callback(panel_id, opt["id"])
                view.add_item(btn)
            await message.edit(view=view)

    def _make_button_callback(self, panel_id: int, option_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.create_ticket(interaction, panel_id, option_id)
        return callback

    async def create_ticket(self, interaction: discord.Interaction, panel_id: int, option_id: int, reason: Optional[str] = None):
        try:
            guild = interaction.guild

            data = await self.db.get_config()
            server_cfg = data.get("server_config", {}).get(str(guild.id), {})

            blocked_role_ids = set(server_cfg.get("blocked_role_ids", []))
            if blocked_role_ids and isinstance(interaction.user, discord.Member):
                user_role_ids = {r.id for r in interaction.user.roles}
                if blocked_role_ids & user_role_ids:
                    return await interaction.followup.send("❌ You're not permitted to open tickets in this server.", ephemeral=True)

            max_tickets = server_cfg.get("max_tickets", 3)
            cooldown_sec = server_cfg.get("cooldown_seconds", 300)

            active_count = 0
            last_created_time = None

            for t in self.ticket_cache.values():
                if t.get("guild_id") == guild.id and t.get("creator_id") == interaction.user.id and t.get("status") == "open":
                    active_count += 1
                    dt = datetime.datetime.fromisoformat(t["created_at"])
                    if not last_created_time or dt > last_created_time:
                        last_created_time = dt

            if active_count >= max_tickets:
                return await interaction.followup.send(f"❌ You already have **{max_tickets} open tickets**. Please close one before opening another.", ephemeral=True)

            if last_created_time:
                now = datetime.datetime.now(datetime.timezone.utc)
                diff = (now - last_created_time).total_seconds()
                if diff < cooldown_sec:
                    remaining = int(cooldown_sec - diff)
                    return await interaction.followup.send(f"⏳ **Cooldown active:** Please wait `{remaining//60}m {remaining%60}s` before opening another ticket.", ephemeral=True)

            panel = data["panels"].get(str(panel_id))
            if not panel:
                await interaction.followup.send("Panel data missing.", ephemeral=True)
                return

            opt = data["options"].get(str(option_id))
            label = opt["label"] if opt else "ticket"

            data["counters"]["tickets"] += 1
            ticket_id = data["counters"]["tickets"]

            clean_label = label.lower().split()[0].replace(' ', '-')
            username = interaction.user.name.lower().replace(' ', '-')
            chan_name = f"🎫-{clean_label}-{username}"

            ticket_mode = server_cfg.get("ticket_mode", "channel")
            staff_role_id = server_cfg.get("staff_role_id")
            ping_role_id = server_cfg.get("ping_role_id")

            if ticket_mode == "thread":
                thread_channel_id = server_cfg.get("thread_channel_id")
                parent = guild.get_channel(thread_channel_id) if thread_channel_id else None
                if not parent:
                    await interaction.followup.send("❌ This server's ticket thread channel is missing or was deleted. An admin needs to run `/setup` again.", ephemeral=True)
                    return
                try:
                    ticket_channel = await parent.create_thread(
                        name=chan_name,
                        type=discord.ChannelType.private_thread,
                        invitable=False,
                        auto_archive_duration=10080,
                        reason=f"Ticket opened by {interaction.user}",
                    )
                    await ticket_channel.add_user(interaction.user)
                except discord.Forbidden:
                    await self.auto_repair_guild_permissions(interaction.guild)
                    await interaction.followup.send("❌ I am missing permissions to create ticket threads. I've attempted an auto-repair, please try again in a moment.", ephemeral=True)
                    return
                except discord.HTTPException as e:
                    await interaction.followup.send(f"Failed to create ticket: {e}", ephemeral=True)
                    return
            else:
                category = guild.get_channel(panel["category_id"])
                if category and len(category.channels) >= 50:
                    i = 2
                    while True:
                        overflow_name = f"{category.name} {i}"
                        existing = discord.utils.get(guild.categories, name=overflow_name)
                        if existing:
                            if len(existing.channels) < 50:
                                category = existing
                                break
                            else:
                                i += 1
                        else:
                            category = await guild.create_category(name=overflow_name, overwrites=category.overwrites)
                            break

                overwrites = {}
                if category:
                    for target, overwrite in category.overwrites.items():
                        overwrites[target] = overwrite

                _ticket_perms = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                _bot_perms    = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)

                overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                overwrites[interaction.user]   = _ticket_perms
                overwrites[guild.me]           = _bot_perms

                for role_id in (staff_role_id, server_cfg.get("staff2_role_id"), server_cfg.get("admin_role_id")):
                    if not role_id:
                        continue
                    role = guild.get_role(role_id)
                    if role:
                        overwrites[role] = _ticket_perms

                if ping_role_id:
                    ping_role = guild.get_role(ping_role_id)
                    if ping_role and ping_role.id not in (staff_role_id, server_cfg.get("staff2_role_id"), server_cfg.get("admin_role_id")):
                        overwrites[ping_role] = _ticket_perms

                try:
                    ticket_channel = await guild.create_text_channel(name=chan_name, category=category, overwrites=overwrites)
                except discord.Forbidden:
                    await self.auto_repair_guild_permissions(interaction.guild)
                    await interaction.followup.send("❌ I am missing the 'Manage Channels' permission to create this ticket. I've attempted an auto-repair, please try again in a moment.", ephemeral=True)
                    raise
                except discord.HTTPException as e:
                    await interaction.followup.send(f"Failed to create ticket: {e}", ephemeral=True)
                    return

            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            ticket_data = {
                "id": int(ticket_id),
                "channel_id": ticket_channel.id,
                "guild_id": guild.id,
                "creator_id": interaction.user.id,
                "panel_id": panel_id,
                "option_id": option_id,
                "created_at": created_at,
                "closed_at": None,
                "claimant_id": None,
                "status": "open"
            }
            await self._save_config(data)
            await self.db.insert_ticket(ticket_data)
            await self.db.save_json_backup(guild.id, ticket_data)
            self.ticket_cache[ticket_channel.id] = ticket_data
            self._mark_dirty(ticket_channel.id)
            self.bot.ui.append_log("", "OK", f"ticket: opened #{ticket_id} ({label}) by {interaction.user} in {guild.name}")

            pings = [interaction.user.mention]
            if staff_role_id:
                pings.append(f"<@&{staff_role_id}>")
            if ping_role_id and ping_role_id != staff_role_id:
                pings.append(f"<@&{ping_role_id}>")
            content = " ".join(pings)

            emoji = opt.get("emoji", "🎫") if opt else "🎫"
            open_dt = datetime.datetime.now(datetime.timezone.utc)
            opened_ts = discord.utils.format_dt(open_dt, style="F")
            icon_url = guild.icon.url if guild.icon else ""
            layout = TicketOpenLayout(
                ticket_id=ticket_id,
                label=label,
                emoji=emoji,
                creator_mention=interaction.user.mention,
                icon_url=icon_url,
                opened_ts=opened_ts,
            )
            await ticket_channel.send(content=content)
            await ticket_channel.send(view=layout)

            repeat_hours = server_cfg.get("repeat_ticket_hours", 72)
            repeat = await self._find_repeat_ticket(guild.id, interaction.user.id, option_id, repeat_hours)
            if repeat:
                closed_dt = datetime.datetime.fromisoformat(repeat["closed_at"])
                await ticket_channel.send(view=PanelView(
                    title="🔁 Repeat Ticket Detected",
                    description=(
                        f"{interaction.user.mention} had ticket **#{repeat['id']}** on this same topic "
                        f"closed {discord.utils.format_dt(closed_dt, 'R')}."
                    ),
                    fields=[("Previous resolution", repeat.get("close_reason") or "No reason recorded.", False)],
                    color=Colors.WARNING,
                ))

            await interaction.followup.send(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

            tc_id = server_cfg.get("transcript_channel_id")
            if tc_id:
                try:
                    tc = guild.get_channel(tc_id) or await self.bot.fetch_channel(tc_id)
                    avatar = interaction.user.display_avatar.url if interaction.user.display_avatar else None
                    await self.safe_send(tc,
                        view=EntryView(
                            emoji="🎫",
                            title="Ticket Opened",
                            target_display=ticket_channel.mention,
                            actor_display=interaction.user.mention,
                            reason=f"Ticket #{ticket_id} — {label}",
                            target_id=ticket_id,
                            color=Colors.SUCCESS,
                            thumbnail_url=avatar,
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except:
                    pass
        except Exception as e:
            await self.report_error("Ticket Creation", e, interaction)
            await interaction.followup.send("❌ An internal error occurred while creating your ticket. The developers have been notified.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        ticket = self.ticket_cache.get(message.channel.id)
        if not ticket or ticket["status"] != "open":
            return

        if "messages" not in ticket:
            ticket["messages"] = []

        attachments = [a.url for a in message.attachments]
        msg_data = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "author_id": message.author.id,
            "author_name": str(message.author),
            "content": message.content,
            "attachments": attachments
        }
        ticket.setdefault("messages", []).append(msg_data)

        if not ticket.get("first_response_at") and message.author.id != ticket.get("creator_id"):
            ticket["first_response_at"] = msg_data["timestamp"]

        await self.db.insert_message(ticket["id"], msg_data)
        self._mark_dirty(message.channel.id)

    async def _send_transcript(self, guild: discord.Guild, ticket: dict, closed_by: Optional[discord.abc.User] = None):
        ticket_id = str(ticket["id"])
        file_path = self.db.get_ticket_path(guild.id, int(ticket["id"]))
        try:
            open_dt = datetime.datetime.fromisoformat(ticket["created_at"])
        except Exception:
            open_dt = datetime.datetime.now(datetime.timezone.utc)
        try:
            closed_dt = datetime.datetime.fromisoformat(ticket["closed_at"]) if ticket.get("closed_at") else datetime.datetime.now(datetime.timezone.utc)
        except Exception:
            closed_dt = datetime.datetime.now(datetime.timezone.utc)

        transcript_panel = PanelView(
            title="📄 Ticket Transcript",
            description=f"Full record of ticket `#{ticket_id}` has been archived.",
            fields=[
                ("🎫 Ticket ID", f"`#{ticket_id}`",                                           True),
                ("👤 Opened By", f"<@{ticket['creator_id']}>",                                True),
                ("🔒 Closed By", closed_by.mention if closed_by else "Auto-closed",           True),
                ("📅 Opened At", discord.utils.format_dt(open_dt, "F"),                       True),
                ("📅 Closed At", discord.utils.format_dt(closed_dt, "F"),                     True),
                ("❓ Resolution", ticket.get("close_reason", "Ticket completed."),             False),
            ],
            color=Colors.NEUTRAL,
            thumbnail_url=guild.icon.url if guild.icon else None,
            footer="Transcript sent to creator via DM",
        )

        cfg = self._cfg(guild.id)
        tc_id = cfg.get("transcript_channel_id")
        if tc_id:
            tc = guild.get_channel(tc_id) or await self.bot.fetch_channel(tc_id)
            await self.safe_send(tc, view=transcript_panel, file=discord.File(file_path) if file_path.exists() else None, allowed_mentions=discord.AllowedMentions.none())

        creator = guild.get_member(ticket["creator_id"]) or await self.bot.fetch_user(ticket["creator_id"])
        if creator:
            try:
                await creator.send(view=transcript_panel, file=discord.File(file_path) if file_path.exists() else None)
            except:
                pass

    async def _run_close_flow(self, channel: discord.TextChannel, ticket: dict, reason: str = "", closed_by: Optional[discord.abc.User] = None):
        """Shared close logic."""
        try:
            guild = self.bot.get_guild(ticket["guild_id"])
            if not guild:
                return

            ticket["status"] = "closed"
            ticket["closed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            ticket["close_reason"] = reason or "Issue resolved."
            await self.db.update_ticket(ticket["id"], status="closed", closed_at=ticket["closed_at"], close_reason=ticket["close_reason"])
            await self.db.save_json_backup(guild.id, ticket)
            self.ticket_cache[channel.id] = ticket
            self._mark_dirty(channel.id)

            closing_text = reason or "No specific reason provided."
            await channel.send(view=TicketClosedLayout(
                ticket_id=ticket["id"],
                creator_id=ticket["creator_id"],
                reason=closing_text,
            ))

            try:
                await self._send_transcript(guild, ticket, closed_by=closed_by)
            except Exception as e:
                await self.report_error("Transcript on Close", e)

            creator = guild.get_member(ticket["creator_id"]) or await self.bot.fetch_user(ticket["creator_id"])
            creator_name = creator.name.lower().replace(' ', '-') if creator else "unknown"
            try:
                await channel.edit(name=f"closed-{creator_name}")
            except:
                pass

            if isinstance(channel, discord.Thread):
                try:
                    creator_member = guild.get_member(ticket["creator_id"])
                    if creator_member:
                        await channel.remove_user(creator_member)
                except:
                    pass
                try:
                    await channel.edit(locked=True, archived=True)
                except:
                    pass
            else:
                try:
                    creator_member = guild.get_member(ticket["creator_id"])
                    if creator_member:
                        await channel.set_permissions(creator_member, overwrite=None)
                except:
                    pass

                try:
                    cfg = self._cfg(guild.id)
                    closed_cat_id = cfg.get("closed_category_id")
                    if closed_cat_id:
                        closed_cat = guild.get_channel(closed_cat_id)
                        if closed_cat:
                            await channel.edit(category=closed_cat, sync_permissions=True)
                except:
                    pass

            try:
                creator = guild.get_member(ticket["creator_id"]) or await self.bot.fetch_user(ticket["creator_id"])
                if creator:
                    gname = guild.name
                    guild_icon = guild.icon.url if guild.icon else None
                    staff_value = f"<@{ticket['claimant_id']}>" if ticket.get("claimant_id") else "Auto-assigned"
                    rating_layout = TicketRatingView(
                        ticket_id=str(ticket["id"]),
                        staff_id=ticket.get("claimant_id"),
                        guild_id=guild.id,
                        guild_name=gname,
                        guild_icon=guild_icon,
                    )
                    await creator.send(view=rating_layout)
            except:
                pass

        except Exception as e:
            await self.report_error("Close Flow", e)

    async def close_ticket_process(self, interaction: discord.Interaction, reason: str = ""):
        try:
            channel = interaction.channel

            ticket = self.ticket_cache.get(channel.id)
            if not ticket or ticket["status"] != "open":
                if not interaction.response.is_done():
                    await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
                else:
                    await interaction.followup.send("This is not an open ticket.", ephemeral=True)
                return

            is_creator = ticket.get("creator_id") == interaction.user.id
            if not is_creator and not self.is_staff_or_owner(interaction):
                msg = "❌ This command is restricted to the ticket creator, staff, or owners."
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
                return

            if not interaction.response.is_done():
                await interaction.response.send_message("Closing ticket...", ephemeral=True)

            default_reason = "Issue resolved and ticket closed by staff."
            if is_creator:
                default_reason = "Ticket closed by the creator."
            ticket["close_reason"] = reason or default_reason
            await self._run_close_flow(channel, ticket, reason=ticket["close_reason"], closed_by=interaction.user)
            self.bot.ui.append_log("", "OK", f"ticket: closed #{ticket['id']} by {interaction.user} {'(creator)' if is_creator else '(staff)'} in {interaction.guild.name}")
        except Exception as e:
            await self.report_error("Ticket Closing", e, interaction)
            try:
                await interaction.followup.send("❌ Error closing ticket. Details reported.", ephemeral=True)
            except:
                pass

    async def process_ticket_deletion(self, interaction: discord.Interaction):
        try:
            if not self.is_staff_or_owner(interaction):
                msg = "❌ This command is restricted to staff or owners."
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
                return

            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)

            channel = interaction.channel
            ticket = self.ticket_cache.get(channel.id)

            if not ticket:
                try:
                    await channel.delete()
                except discord.Forbidden:
                    await self.auto_repair_guild_permissions(interaction.guild)
                    await interaction.followup.send("❌ I do not have permission to delete this channel. I've attempted an auto-repair, please try again.", ephemeral=True)
                return

            ticket_id = str(ticket["id"])
            self.ticket_cache.pop(channel.id, None)

            if ticket["status"] == "open":
                ticket["status"] = "closed"
                ticket["closed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                await self.db.update_ticket(ticket["id"], status="closed", closed_at=ticket["closed_at"])
                await self.db.save_json_backup(interaction.guild.id, ticket)
                self._mark_dirty(channel.id)

            await self._send_transcript(interaction.guild, ticket, closed_by=interaction.user)

            try:
                await channel.delete()
                self.bot.ui.append_log("", "OK", f"ticket: deleted #{ticket_id} by {interaction.user} in {interaction.guild.name}")
            except discord.Forbidden:
                await self.auto_repair_guild_permissions(interaction.guild)
                await interaction.followup.send("❌ I am missing the 'Manage Channels' permission to delete this channel. I've attempted an auto-repair, please try again.", ephemeral=True)
        except Exception as e:
            await self.report_error("Ticket Deletion", e, interaction)
            try: await interaction.followup.send("❌ Error deleting channel.")
            except: pass

    async def generate_transcript_action(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
            channel = interaction.channel
            ticket = self.ticket_cache.get(channel.id)

            if not ticket:
                await interaction.followup.send(view=PanelView(description="❌ Ticket data could not be found for this channel.", color=Colors.ERROR), ephemeral=True)
                return

            ticket_id = str(ticket["id"])

            file_path = self.db.get_ticket_path(interaction.guild.id, int(ticket["id"]))
            if not file_path.exists():
                await interaction.followup.send(view=PanelView(description="❌ No transcript data is available for this ticket yet.", color=Colors.ERROR), ephemeral=True)
                return

            await interaction.followup.send(
                view=PanelView(title="📄 Transcript Ready", description="Your ticket transcript has been generated and is attached below.", color=Colors.PRIMARY),
                file=discord.File(file_path),
            )
            self.bot.ui.append_log("", "OK", f"ticket: transcript generated for #{ticket_id} by {interaction.user} in {interaction.guild.name}")
        except Exception as e:
            await self.report_error("Transcript Generation", e, interaction)
            await interaction.followup.send("❌ Error generating transcript.")

    async def reopen_ticket_process(self, interaction: discord.Interaction):
        try:
            if not self.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)

            await interaction.response.defer()
            channel = interaction.channel
            ticket = self.ticket_cache.get(channel.id)

            if not ticket:
                ticket = await self.db.find_ticket_by_channel(channel.id)
                if not ticket:
                    await interaction.followup.send("❌ Ticket data not found for this channel.", ephemeral=True)
                    return

            ticket_id = str(ticket["id"])
            ticket["status"] = "open"
            ticket["closed_at"] = None
            await self.db.update_ticket(ticket["id"], status="open", closed_at=None)
            await self.db.save_json_backup(interaction.guild.id, ticket)
            self._mark_dirty(channel.id)
            self.bot.ui.append_log("", "OK", f"ticket: reopened #{ticket_id} by {interaction.user} in {interaction.guild.name}")

            data = await self.db.get_config()
            opt = data["options"].get(str(ticket["option_id"]))
            label = opt["label"] if opt else "ticket"
            clean_label = label.lower().split()[0].replace(' ', '-')
            creator = interaction.guild.get_member(ticket["creator_id"]) or await self.bot.fetch_user(ticket["creator_id"])
            username = creator.name.lower().replace(' ', '-') if creator else "unknown"
            original_name = f"🎫-{clean_label}-{username}"

            creator_member = interaction.guild.get_member(ticket["creator_id"])
            if isinstance(channel, discord.Thread):
                try:
                    await channel.edit(name=original_name, archived=False, locked=False)
                except:
                    pass
                if creator_member:
                    try:
                        await channel.add_user(creator_member)
                    except:
                        pass
            else:
                try:
                    await channel.edit(name=original_name)
                except:
                    pass
                if creator_member:
                    await channel.set_permissions(creator_member, view_channel=True, send_messages=True, read_message_history=True)

            await channel.send(view=PanelView(
                title="🔓 Ticket Reopened",
                description=f"This ticket has been **reopened** by {interaction.user.mention}.\nThe original creator now has access to this channel again.",
                color=Colors.SUCCESS,
            ))
            await interaction.message.delete()
        except Exception as e:
            await self.report_error("Ticket Reopening", e, interaction)
            await interaction.followup.send("❌ Error reopening ticket.")

    @ticket.command(name="transcript", description="Generate a transcript of the current ticket.")
    async def transcript_cmd(self, interaction: discord.Interaction):
        ticket = self.ticket_cache.get(interaction.channel.id)

        if not ticket:
            await interaction.response.send_message("❌ This channel is not a ticket.", ephemeral=True)
            return

        is_creator = ticket.get("creator_id") == interaction.user.id
        if not is_creator and not self.is_staff_or_owner(interaction):
            await interaction.response.send_message("❌ This command is restricted to the ticket creator, staff, or owners.", ephemeral=True)
            return

        file_path = self.db.get_ticket_path(interaction.guild.id, int(ticket["id"]))

        if not file_path.exists():
            await interaction.response.send_message("No transcript messages found.", ephemeral=True)
            return

        await interaction.response.send_message(file=discord.File(file_path), ephemeral=True)

    @ticket.command(name="add", description="Add a member to the current ticket.")
    @app_commands.describe(member="The member to add")
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        ticket = self.ticket_cache.get(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)

        await self._grant_access(interaction.channel, member)

        await interaction.response.send_message(view=PanelView(description=f"✅ {member.mention} has been added to this ticket.", color=Colors.SUCCESS))

    @ticket.command(name="remove", description="Remove a member from the current ticket.")
    @app_commands.describe(member="The member to remove")
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        ticket = self.ticket_cache.get(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)

        await self._revoke_access(interaction.channel, member)

        await interaction.response.send_message(view=PanelView(description=f"✅ {member.mention} has been removed from this ticket.", color=Colors.DANGER))

    async def auto_repair_guild_permissions(self, guild: discord.Guild, data: Optional[dict] = None):
        """Silently attempts to repair permissions for all configured ticket channels in a guild."""
        try:
            if not data:
                data = await self.db.get_config()
            server_cfg = data.get("server_config", {}).get(str(guild.id), {})

            async def fix_perms(channel):
                if not channel: return
                try:
                    if isinstance(channel, (discord.TextChannel, discord.CategoryChannel)):
                        await channel.set_permissions(
                            guild.me,
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            manage_channels=True,
                            manage_messages=True
                        )
                except: pass

            tc_id = server_cfg.get("transcript_channel_id")
            if tc_id:
                await fix_perms(guild.get_channel(tc_id) or await self.bot.fetch_channel(tc_id))

            cc_id = server_cfg.get("closed_category_id")
            if cc_id:
                await fix_perms(guild.get_channel(cc_id) or await self.bot.fetch_channel(cc_id))

            esc_id = server_cfg.get("escalation_channel_id")
            if esc_id:
                await fix_perms(guild.get_channel(esc_id) or await self.bot.fetch_channel(esc_id))

            for pid, panel in data.get("panels", {}).items():
                cat_id = panel.get("category_id")
                if cat_id:
                    try:
                        cat = guild.get_channel(cat_id) or await self.bot.fetch_channel(cat_id)
                        if cat and cat.guild.id == guild.id:
                            await fix_perms(cat)
                    except: pass

            guild_tickets_dir = self.db.tickets_dir / str(guild.id)
            if guild_tickets_dir.exists():
                for ticket_file in guild_tickets_dir.glob("*.json"):
                    try:
                        t = json.loads(ticket_file.read_text(encoding="utf-8"))
                        if t.get("status") == "open":
                            chan = guild.get_channel(t["channel_id"]) or guild.get_thread(t["channel_id"])
                            if chan and not isinstance(chan, discord.Thread):
                                await fix_perms(chan)
                    except: continue
        except Exception as e:
            print(f"[REPAIR] Error during auto-repair for guild {guild.id}: {e}")

    async def claim_ticket_process(self, interaction: discord.Interaction):
        try:
            if not self.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ You do not have permission to claim this ticket.", ephemeral=True)

            cfg = self._cfg(interaction.guild.id)
            ticket = self.ticket_cache.get(interaction.channel.id)

            if not ticket:
                return await interaction.response.send_message("❌ Error: Ticket not found in database.", ephemeral=True)

            if ticket["claimant_id"]:
                return await interaction.response.send_message(f"❌ This ticket is already claimed by <@{ticket['claimant_id']}>.", ephemeral=True)

            ticket["claimant_id"] = interaction.user.id
            await self.db.update_ticket(ticket["id"], claimant_id=interaction.user.id)
            await self.db.save_json_backup(interaction.guild.id, ticket)
            self._mark_dirty(interaction.channel.id)
            self.bot.ui.append_log("", "OK", f"ticket: #{ticket['id']} claimed by {interaction.user} in {interaction.guild.name}")

            await self._grant_access(interaction.channel, interaction.user)

            if not isinstance(interaction.channel, discord.Thread):
                staff2_id = cfg.get("staff2_role_id")
                staff2 = interaction.guild.get_role(staff2_id) if staff2_id else None
                if staff2:
                    await interaction.channel.set_permissions(staff2, overwrite=None)

                ping_role_id = cfg.get("ping_role_id")
                if ping_role_id:
                    ping_role = interaction.guild.get_role(ping_role_id)
                    if ping_role and ping_role.id != cfg.get("staff_role_id"):
                        await interaction.channel.set_permissions(ping_role, overwrite=None)

            creator = interaction.guild.get_member(ticket["creator_id"])
            await self._grant_access(interaction.channel, creator)

            await interaction.response.send_message(f"✅ You have claimed this ticket, {interaction.user.mention}!", ephemeral=False)
            try:
                fresh = discord.ui.LayoutView.from_message(interaction.message, timeout=None)
                for item in fresh.children:
                    if isinstance(item, discord.ui.Container):
                        item.accent_colour = Colors.SUCCESS
                for item in fresh.walk_children():
                    if isinstance(item, discord.ui.TextDisplay) and "Open — Awaiting Staff" in item.content:
                        item.content = item.content.replace(
                            "**📋 Status:** `Open — Awaiting Staff`",
                            f"**📋 Status:** `Claimed by {interaction.user.display_name}`"
                        )
                        break
                await interaction.message.edit(view=fresh)
                self.bot.add_view(TicketOpenLayout(), message_id=interaction.message.id)
            except Exception:
                pass
            self.ticket_cache[interaction.channel.id] = ticket
        except Exception as e:
            await self.report_error("Ticket Claim", e, interaction)
            await interaction.response.send_message("❌ Error claiming ticket.", ephemeral=True)

    @ticket.command(name="repair", description="Audits and repairs bot permissions in all ticket-related channels.")
    async def ticket_repair(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.is_staff_or_owner(interaction):
            return await interaction.followup.send("❌ This command is restricted to staff or owners.", ephemeral=True)
        try:
            guild = interaction.guild
            data = await self.db.get_config()
            server_cfg = data.get("server_config", {}).get(str(guild.id), {})

            repaired = []
            failed = []

            tc_id = server_cfg.get("transcript_channel_id")
            if tc_id:
                try:
                    tc = guild.get_channel(tc_id) or await self.bot.fetch_channel(tc_id)
                    if tc:
                        await tc.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
                        repaired.append(f"Transcript Channel: {tc.name}")
                except:
                    failed.append("Transcript Channel (Not found or No Perms)")

            cc_id = server_cfg.get("closed_category_id")
            if cc_id:
                try:
                    cc = guild.get_channel(cc_id) or await self.bot.fetch_channel(cc_id)
                    if cc:
                        await cc.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                        repaired.append(f"Closed Category: {cc.name}")
                except:
                    failed.append("Closed Category (Not found or No Perms)")

            for pid, panel in data.get("panels", {}).items():
                cat_id = panel.get("category_id")
                if cat_id:
                    try:
                        cat = guild.get_channel(cat_id) or await self.bot.fetch_channel(cat_id)
                        if cat and cat.guild.id == guild.id:
                            await cat.set_permissions(guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
                            repaired.append(f"Panel Category ({panel['name']}): {cat.name}")
                    except:
                        failed.append(f"Panel Category ({panel['name']}) (Not found)")

            admin_status = "✅ Enabled" if guild.me.guild_permissions.administrator else "❌ Disabled (Standard Perms)"
            repair_fields = [("🛡️ Global Administrator", admin_status, False)]
            if repaired:
                repair_fields.append(("✅ Repaired/Verified", "\n".join(repaired[:10]), False))
            if failed:
                repair_fields.append(("❌ Failed/Missing", "\n".join(failed[:10]), False))
            desc = "No ticket channels found to repair. Have you run `/setup` yet?" if not repaired and not failed else f"Audited **{len(repaired) + len(failed)}** channels across the server."
            await interaction.followup.send(view=PanelView(
                title="🔧 Permission Repair Audit",
                description=desc,
                fields=repair_fields,
                color=Colors.PRIMARY,
            ), ephemeral=True)
        except Exception as e:
            await self.report_error("Ticket Repair", e, interaction)
            await interaction.followup.send(f"❌ Repair failed: {e}", ephemeral=True)

    async def escalate_ticket_process(self, interaction: discord.Interaction):
        try:
            if not self.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)

            server_cfg = self._cfg(interaction.guild.id)
            admin_role_id = server_cfg.get("admin_role_id")
            esc_chan_id = server_cfg.get("escalation_channel_id")

            if not admin_role_id:
                return await interaction.response.send_message("❌ No admin/higher-up role configured for escalation. Run `/setup` with `admin_role:` set.", ephemeral=True)

            admin_role = interaction.guild.get_role(admin_role_id)
            if not admin_role:
                return await interaction.response.send_message("❌ The configured admin role no longer exists.", ephemeral=True)

            await self._grant_role_access(interaction.channel, admin_role)
            ticket = self.ticket_cache.get(interaction.channel.id)
            tid = f"#{ticket['id']}" if ticket else interaction.channel.name
            self.bot.ui.append_log("", "WARN", f"ticket: {tid} escalated by {interaction.user} → {admin_role.name} in {interaction.guild.name}")

            await interaction.channel.send(content=admin_role.mention, allowed_mentions=discord.AllowedMentions(roles=False))
            await interaction.channel.send(view=PanelView(
                title="⬆️ Ticket Escalated",
                description=f"This ticket has been escalated to the {admin_role.mention} team by {interaction.user.mention}.",
                color=Colors.PINK,
            ))
            await interaction.response.send_message("✅ Ticket escalated successfully.", ephemeral=True)

            if esc_chan_id:
                esc_chan = self.bot.get_channel(esc_chan_id) or await self.bot.fetch_channel(esc_chan_id)
                await self.safe_send(esc_chan, view=PanelView(
                    title="⬆️ Ticket Escalated",
                    description=f"Ticket: {interaction.channel.mention}\nEscalated by: {interaction.user.mention}",
                    color=Colors.PINK,
                ))
        except Exception as e:
            await self.report_error("Ticket Escalation", e, interaction)
            await interaction.response.send_message("❌ Error escalating ticket.", ephemeral=True)

    async def transfer_ticket_process(self, interaction: discord.Interaction, role: discord.Role):
        try:
            if not self.is_staff_or_owner(interaction):
                return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)

            ticket = self.ticket_cache.get(interaction.channel.id)
            if not ticket:
                return await interaction.response.send_message("❌ Error: Ticket not found in database.", ephemeral=True)

            cfg = self._cfg(interaction.guild.id)

            if ticket.get("claimant_id"):
                old_claimant = interaction.guild.get_member(ticket["claimant_id"])
                if old_claimant and old_claimant.id not in OWNER_IDS:
                    staff_role_id = cfg.get("staff_role_id")
                    has_main_staff = staff_role_id and any(r.id == staff_role_id for r in old_claimant.roles)
                    if not has_main_staff:
                        await self._revoke_access(interaction.channel, old_claimant)

            ticket["claimant_id"] = None
            await self.db.update_ticket(ticket["id"], claimant_id=None)
            await self.db.save_json_backup(interaction.guild.id, ticket)
            self._mark_dirty(interaction.channel.id)

            await self._grant_role_access(interaction.channel, role)

            await interaction.response.edit_message(content=f"✅ Ticket transferred to {role.mention}.", view=None)

            await interaction.channel.send(content=role.mention, allowed_mentions=discord.AllowedMentions(roles=False))
            await interaction.channel.send(view=PanelView(
                title="🔀 Ticket Transferred",
                description=f"This ticket has been transferred to {role.mention} by {interaction.user.mention}.",
                color=Colors.INFO,
            ))
        except Exception as e:
            await self.report_error("Ticket Transfer", e, interaction)
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Error transferring ticket.", ephemeral=True)

    @option.command(name="add", description="Add a new option to a ticket panel.")
    @app_commands.describe(panel_id="Panel ID number", label="Button label", emoji="Optional emoji", description="Optional description")
    async def option_add(self, interaction: discord.Interaction, panel_id: int, label: str, emoji: str = None, description: str = None):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        data = await self.db.get_config()
        panel = data["panels"].get(str(panel_id))
        if not panel:
            return await interaction.response.send_message(f"❌ Panel `{panel_id}` not found.", ephemeral=True)
        data["counters"]["options"] += 1
        opt_id = str(data["counters"]["options"])
        data["options"][opt_id] = {
            "id": int(opt_id), "panel_id": panel_id,
            "label": label, "emoji": emoji or "", "description": description or ""
        }
        await self._save_config(data)
        await self.refresh_panel_message(interaction.guild, panel_id)
        await interaction.response.send_message(f"✅ Option `{label}` added to panel `{panel_id}`.", ephemeral=True)

    @option.command(name="edit", description="Edit an existing ticket option.")
    @app_commands.describe(option_id="Option ID number", label="New label", emoji="New emoji", description="New description")
    async def option_edit(self, interaction: discord.Interaction, option_id: int, label: str = None, emoji: str = None, description: str = None):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        data = await self.db.get_config()
        opt = data["options"].get(str(option_id))
        if not opt:
            return await interaction.response.send_message(f"❌ Option `{option_id}` not found.", ephemeral=True)
        if label: opt["label"] = label
        if emoji is not None: opt["emoji"] = emoji
        if description is not None: opt["description"] = description
        await self._save_config(data)
        await self.refresh_panel_message(interaction.guild, opt["panel_id"])
        await interaction.response.send_message(f"✅ Option `{option_id}` updated.", ephemeral=True)

    @option.command(name="remove", description="Remove an option from a panel.")
    @app_commands.describe(option_id="Option ID number")
    async def option_remove(self, interaction: discord.Interaction, option_id: int):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        data = await self.db.get_config()
        opt = data["options"].pop(str(option_id), None)
        if not opt:
            return await interaction.response.send_message(f"❌ Option `{option_id}` not found.", ephemeral=True)
        await self._save_config(data)
        await self.refresh_panel_message(interaction.guild, opt["panel_id"])
        await interaction.response.send_message(f"✅ Option `{option_id}` removed.", ephemeral=True)

    @ticket.command(name="panel", description="Re-send a ticket panel to the current channel.")
    @app_commands.describe(panel_id="Panel ID to resend")
    async def ticket_panel_resend(self, interaction: discord.Interaction, panel_id: int):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        data = await self.db.get_config()
        panel = data["panels"].get(str(panel_id))
        if not panel:
            return await interaction.response.send_message(f"❌ Panel `{panel_id}` not found.", ephemeral=True)
        options = [opt for opt in data["options"].values() if str(opt["panel_id"]) == str(panel_id)]
        if not options:
            return await interaction.response.send_message("❌ This panel has no options.", ephemeral=True)

        thumb_url = interaction.guild.icon.url if interaction.guild.icon else None
        if panel["type"] == "dropdown":
            panel_layout = TicketPanelLayout(panel_id, options, panel_type="dropdown", thumb=thumb_url)
        else:
            panel_layout = TicketPanelLayout(
                panel_id, options, panel_type="button", thumb=thumb_url,
                callback_factory=self._make_button_callback,
            )
        msg = await interaction.channel.send(view=panel_layout)
        data["panels"][str(panel_id)]["message_id"] = msg.id
        data["panels"][str(panel_id)]["channel_id"] = interaction.channel.id
        await self._save_config(data)
        await interaction.response.send_message(f"✅ Panel re-sent to {interaction.channel.mention}.", ephemeral=True)

    @ticket.command(name="rename", description="Rename the current ticket channel.")
    @app_commands.describe(name="New channel name")
    async def ticket_rename(self, interaction: discord.Interaction, name: str):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        ticket = self.ticket_cache.get(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        clean = name.lower().replace(" ", "-")[:32]
        await interaction.channel.edit(name=clean)
        await interaction.response.send_message(f"✅ Channel renamed to `{clean}`.", ephemeral=True)

    @ticket.command(name="close", description="Close the current ticket (staff fallback).")
    @app_commands.describe(reason="Optional closing reason")
    async def close_command(self, interaction: discord.Interaction, reason: str = None):
        ticket = self.ticket_cache.get(interaction.channel.id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("❌ This is not an open ticket channel.", ephemeral=True)
        is_creator = ticket.get("creator_id") == interaction.user.id
        if not is_creator and not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to the ticket creator, staff, or owners.", ephemeral=True)
        await self.close_ticket_process(interaction, reason or "Closed via /close command.")

    @ticket.command(name="config", description="Configure ticket settings.")
    @app_commands.describe(
        max_tickets="Max open tickets per user (0 = unlimited)",
        cooldown_seconds="Cooldown between tickets in seconds",
        inactivity_hours="Auto-close tickets after N hours of inactivity (0 = disable)",
        sla_minutes="Remind staff if no one replies within N minutes of a ticket opening (0 = disable)",
        repeat_ticket_hours="Flag a new ticket as a repeat if the same user closed one on the same topic within N hours (0 = disable)",
        block_role="Prevent members with this role from opening new tickets",
        unblock_role="Allow members with this role to open tickets again",
    )
    async def ticket_config(self, interaction: discord.Interaction, max_tickets: int = None, cooldown_seconds: int = None, inactivity_hours: int = None, sla_minutes: int = None, repeat_ticket_hours: int = None, block_role: discord.Role = None, unblock_role: discord.Role = None):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        data = await self.db.get_config()
        cfg = data.setdefault("server_config", {}).setdefault(str(interaction.guild.id), {})
        changes = []
        if max_tickets is not None:
            cfg["max_tickets"] = max(max_tickets, 0)
            changes.append(f"max_tickets → `{cfg['max_tickets']}`")
        if cooldown_seconds is not None:
            cfg["cooldown_seconds"] = max(cooldown_seconds, 0)
            changes.append(f"cooldown → `{cfg['cooldown_seconds']}s`")
        if inactivity_hours is not None:
            cfg["inactivity_hours"] = max(inactivity_hours, 0)
            changes.append(f"inactivity_auto_close → `{cfg['inactivity_hours']}h`")
        if sla_minutes is not None:
            cfg["sla_minutes"] = max(sla_minutes, 0)
            changes.append(f"sla_reminder → `{cfg['sla_minutes']}m`")
        if repeat_ticket_hours is not None:
            cfg["repeat_ticket_hours"] = max(repeat_ticket_hours, 0)
            changes.append(f"repeat_ticket_window → `{cfg['repeat_ticket_hours']}h`")
        if block_role is not None:
            blocked = set(cfg.get("blocked_role_ids", []))
            blocked.add(block_role.id)
            cfg["blocked_role_ids"] = list(blocked)
            changes.append(f"blocked {block_role.mention}")
        if unblock_role is not None:
            blocked = set(cfg.get("blocked_role_ids", []))
            blocked.discard(unblock_role.id)
            cfg["blocked_role_ids"] = list(blocked)
            changes.append(f"unblocked {unblock_role.mention}")
        await self._save_config(data)
        blocked_mentions = ", ".join(f"<@&{rid}>" for rid in cfg.get("blocked_role_ids", [])) or "None"
        current = (
            f"**Current:** max_tickets=`{cfg.get('max_tickets', 3)}`, "
            f"cooldown=`{cfg.get('cooldown_seconds', 300)}s`, "
            f"inactivity_auto_close=`{cfg.get('inactivity_hours', 48)}h`, "
            f"sla_reminder=`{cfg.get('sla_minutes', 15)}m`, "
            f"repeat_ticket_window=`{cfg.get('repeat_ticket_hours', 72)}h`, "
            f"blocked_roles={blocked_mentions}"
        )
        await interaction.response.send_message(f"✅ {' | '.join(changes) if changes else 'No changes.'}\n{current}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="roles", description="Show which roles/channels are configured for this server's ticket system.")
    async def ticket_roles(self, interaction: discord.Interaction):
        if not self.is_staff_or_owner(interaction):
            return await interaction.response.send_message("❌ This command is restricted to staff or owners.", ephemeral=True)
        cfg = self._cfg(interaction.guild.id)

        def fmt_role(key: str) -> str:
            role_id = cfg.get(key)
            if not role_id:
                return "*Not set*"
            if role_id == interaction.guild.id:
                return "⚠️ **@everyone** — this is a bug, re-run `/setup` with a real role"
            role = interaction.guild.get_role(role_id)
            return role.mention if role else f"⚠️ Role `{role_id}` no longer exists"

        def fmt_channel(key: str) -> str:
            chan_id = cfg.get(key)
            if not chan_id:
                return "*Not set*"
            chan = interaction.guild.get_channel(chan_id)
            return chan.mention if chan else f"⚠️ Channel `{chan_id}` no longer exists"

        fields = [
            ("Ticket mode", f"`{cfg.get('ticket_mode', 'channel')}`", True),
            ("Staff role", fmt_role("staff_role_id"), True),
            ("Staff role 2", fmt_role("staff2_role_id"), True),
            ("Admin role", fmt_role("admin_role_id"), True),
            ("Ping role", fmt_role("ping_role_id"), True),
            ("Transcript channel", fmt_channel("transcript_channel_id"), True),
            ("Closed category", fmt_channel("closed_category_id"), True),
            ("Escalation channel", fmt_channel("escalation_channel_id"), True),
            ("Review channel", fmt_channel("review_channel_id"), True),
            ("Error channel", fmt_channel("error_channel_id"), True),
        ]
        if cfg.get("ticket_mode") == "thread":
            fields.append(("Thread channel", fmt_channel("thread_channel_id"), True))

        blocked = cfg.get("blocked_role_ids", [])
        blocked_str = ", ".join(f"<@&{rid}>" for rid in blocked) if blocked else "*None*"
        fields.append(("Blocked roles", blocked_str, False))

        await interaction.response.send_message(view=PanelView(
            title="🔧 Ticket System Configuration",
            description=f"Current settings for **{interaction.guild.name}**.",
            fields=fields,
            color=Colors.PRIMARY,
        ), ephemeral=True)

    @ticket.command(name="bulk_transcript", description="Export all ticket transcripts as a single file.")
    async def ticket_bulk_transcript(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.is_staff_or_owner(interaction):
            return await interaction.followup.send("❌ This command is restricted to staff or owners.", ephemeral=True)
        guild_dir = self.db.tickets_dir / str(interaction.guild.id)
        if not guild_dir.exists():
            return await interaction.followup.send("❌ No tickets found for this guild.", ephemeral=True)

        lines = []
        for ticket_file in sorted(guild_dir.glob("*.json"), key=lambda p: int(p.stem)):
            try:
                t = json.loads(ticket_file.read_text(encoding="utf-8"))
                lines.append(f"=== Ticket #{t['id']} | Status: {t.get('status', '?')} | Created: {t.get('created_at', '?')} ===")
                lines.append(f"Creator: {t.get('creator_id', '?')} | Claimant: {t.get('claimant_id', 'None')}")
                for msg in t.get("messages", []):
                    lines.append(f"[{msg['timestamp']}] {msg['author_name']}: {msg['content']}")
                    for url in msg.get("attachments", []):
                        lines.append(f"  └ Attachment: {url}")
                lines.append("")
            except:
                continue

        content = "\n".join(lines) or "No ticket data found."
        temp = Path(self.db.base_path) / "_bulk_export.txt"
        temp.write_text(content, encoding="utf-8")
        await interaction.followup.send(file=discord.File(temp), ephemeral=True)
        temp.unlink(missing_ok=True)

    @app_commands.command(name="reviews", description="See public support ratings and reviews.")
    async def reviews(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            stats = await self.db.get_review_stats(interaction.guild.id)
            recent = await self.db.get_recent_reviews(interaction.guild.id)

            if stats["total"] == 0:
                return await interaction.followup.send(view=PanelView(
                    title="⭐ Support Reviews",
                    description="No reviews yet. Be the first to rate after your next ticket!",
                    color=Colors.PRIMARY,
                ), ephemeral=True)

            stars = round(stats["avg"])
            bar = "⭐" * stars + "☆" * (5 - stars)
            dist = stats["distribution"]
            dist_str = "\n".join(
                f"{'⭐' * r}: {'█' * max(1, dist[r])} ({dist[r]})" if dist[r] else f"{'⭐' * r}: `0`"
                for r in range(5, 0, -1)
            )
            review_fields = [
                ("⭐ Average Rating", f"{bar} **{stats['avg']}/5**", False),
                ("📊 Total Reviews",  f"`{stats['total']}`",         True),
                ("📈 Distribution",   dist_str,                       False),
            ]
            if recent:
                recent_lines = []
                for r in recent:
                    s = "⭐" * r["rating"]
                    staff = f"<@{r['staff_id']}>" if r["staff_id"] else "Auto"
                    recent_lines.append(f"• Ticket `#{r['ticket_id']}` — {s} — {staff}")
                review_fields.append(("🕐 Recent Reviews", "\n".join(recent_lines[:5]), False))
            await interaction.followup.send(view=PanelView(
                title="⭐ Support Reviews",
                fields=review_fields,
                color=Colors.GOLD,
            ), ephemeral=True)
        except Exception as e:
            await self.report_error("Reviews Command", e, interaction)
            await interaction.followup.send("❌ An error occurred while fetching reviews.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
