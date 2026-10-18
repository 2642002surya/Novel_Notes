import asyncio
import datetime
import logging
import math
import os
import time
import discord
from discord.ext import commands, tasks

from core.database import (
    mark_messages_scanned, 
    update_scan_timestamp,
    ignore_novel, unignore_novel, get_ignored_slugs
)
from core.utils import (
    URL_REGEX, is_valid_novel_url, format_display_path,
    extract_novel_slug, slug_to_display_title,
    are_novels_matching, is_channel_named_after_novel,
    extract_chapter_number, is_protected_channel
)

logger = logging.getLogger("NovelScanner")

COLOR_PRIMARY = discord.Color.from_rgb(88, 101, 242)
COLOR_SUCCESS = discord.Color.from_rgb(87, 242, 135)
COLOR_WARNING = discord.Color.from_rgb(254, 231, 92)
COLOR_DANGER = discord.Color.from_rgb(237, 66, 69)

# 5:30 AM IST = 00:00 UTC
DAILY_SCAN_TIME_UTC = datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc)

def get_target_guild_ids() -> set[int] | None:
    """
    Parses SERVERS from .env (comma-separated).
    Returns a set of guild IDs, or None if not specified (allowing all).
    """
    raw = os.getenv("SERVERS", "").strip()
    if not raw:
        return None
    ids = set()
    for s in raw.split(","):
        clean_s = s.strip()
        if clean_s.isdigit():
            ids.add(int(clean_s))
    return ids if ids else None

def is_guild_allowed(guild_id: int) -> bool:
    target_ids = get_target_guild_ids()
    if target_ids is None:
        return True
    return guild_id in target_ids

class NovelItem:
    def __init__(self, url: str, message: discord.Message):
        self.url = url
        self.message_id = message.id
        self.channel_id = message.channel.id
        self.channel_name = message.channel.name
        self.category_name = message.channel.category.name if message.channel.category else None
        self.is_protected = is_protected_channel(message.channel)
        self.chapter_num = extract_chapter_number(url)
        self.jump_url = message.jump_url
        self.author_id = message.author.id
        self.author_tag = str(message.author)
        self.created_at = message.created_at

class PaginatedNovelClusterView(discord.ui.View):
    PAGE_SIZE = 3

    def __init__(self, canonical_slug: str, novel_title: str, items: list[NovelItem], author_id: int, guild_id: int):
        super().__init__(timeout=400)
        self.canonical_slug = canonical_slug
        self.novel_title = novel_title
        self.items = items
        self.author_id = author_id
        self.guild_id = guild_id
        self.current_page = 0
        self.total_pages = math.ceil(len(items) / self.PAGE_SIZE)
        self.rebuild_ui()

    def create_embed(self) -> discord.Embed:
        start = self.current_page * self.PAGE_SIZE
        page_items = self.items[start:start + self.PAGE_SIZE]
        has_protected = any(item.is_protected for item in self.items)

        embed = discord.Embed(
            title=f"📚 Duplicate Detected: {self.novel_title}",
            description=(
                f"Located **{len(self.items)}** cross-posted links across non-dedicated channels.\n"
                f"Page `{self.current_page + 1}/{self.total_pages}` • Instances `{start + 1}-{start + len(page_items)}`\n"
                "────────────────────────────────────────────"
            ),
            color=COLOR_DANGER if has_protected else COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        if has_protected:
            embed.add_field(
                name="🛡️ Protected Category Notice",
                value="One or more links were found in **Completed** or **Long novels**. These messages cannot be deleted by the bot.",
                inline=False
            )

        max_ch = max((item.chapter_num for item in self.items), default=-1.0)

        for i, item in enumerate(page_items, start=1):
            idx = start + i
            ts = int(item.created_at.timestamp())
            display_path = format_display_path(item.url)
            
            status_badges = []
            if item.chapter_num != -1.0:
                if item.chapter_num == max_ch and len(self.items) > 1:
                    status_badges.append(f"🟢 **Ch. {item.chapter_num:g} (Latest)**")
                else:
                    status_badges.append(f"Ch. {item.chapter_num:g}")
            if item.is_protected:
                status_badges.append("🛡️ **PROTECTED CATEGORY**")

            badge_str = f" • {' | '.join(status_badges)}" if status_badges else ""
            cat_str = f" `{item.category_name}` > " if item.category_name else " "

            embed.add_field(
                name=f"Link #{idx}{badge_str}",
                value=(
                    f"🔗 **URL:** [{display_path}]({item.url})\n"
                    f"📍 **Channel:**{cat_str}<#{item.channel_id}> (`#{item.channel_name}`)\n"
                    f"👤 **Author:** <@{item.author_id}> • <t:{ts}:R>\n"
                    f"🚀 [Jump to message]({item.jump_url})"
                ),
                inline=False
            )

        embed.set_footer(text="Actions: View jumps • Delete removes • Sweep keeps newest/latest • Ignore silences novel")
        return embed

    def rebuild_ui(self):
        self.clear_items()
        start = self.current_page * self.PAGE_SIZE
        page_items = self.items[start:start + self.PAGE_SIZE]

        # Row 0: View Link Buttons
        for i, item in enumerate(page_items, start=1):
            idx = start + i
            self.add_item(discord.ui.Button(
                label=f"View {idx}",
                url=item.jump_url,
                style=discord.ButtonStyle.link,
                row=0
            ))

        # Row 1: Delete Message Buttons
        for i, item in enumerate(page_items, start=1):
            idx = start + i
            self.add_item(DeleteButton(
                item=item,
                display_num=idx,
                parent_view=self,
                row=1
            ))

        # Row 2: Navigation Buttons
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page == 0),
                row=2
            )
            prev_btn.callback = self.prev_page_callback
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.current_page == self.total_pages - 1),
                row=2
            )
            next_btn.callback = self.next_page_callback
            self.add_item(next_btn)

        # Row 3: Sweep & Ignore Actions
        sweep_btn = discord.ui.Button(
            label="Sweep Duplicates (Keep Latest)",
            style=discord.ButtonStyle.danger,
            emoji="🧹",
            row=3
        )
        sweep_btn.callback = self.sweep_duplicates_callback
        self.add_item(sweep_btn)

        ignore_btn = discord.ui.Button(
            label="Ignore Novel",
            style=discord.ButtonStyle.secondary,
            emoji="🔕",
            row=3
        )
        ignore_btn.callback = self.ignore_novel_callback
        self.add_item(ignore_btn)

    async def prev_page_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the runner or moderators can paginate.", ephemeral=True)
        if self.current_page > 0:
            self.current_page -= 1
            self.rebuild_ui()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def next_page_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id and not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Only the runner or moderators can paginate.", ephemeral=True)
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.rebuild_ui()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def ignore_novel_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("You need `Manage Messages` to ignore novels.", ephemeral=True)

        await ignore_novel(self.guild_id, self.canonical_slug)
        for item in self.children:
            item.disabled = True
        
        ignored_embed = discord.Embed(
            title=f"🔕 Ignored: {self.novel_title}",
            description=f"This novel has been marked to be ignored. Future scans and automatic alerts will skip it.\n\n*To undo, use:* `i novel unignore {self.canonical_slug}`",
            color=COLOR_SUCCESS
        )
        await interaction.message.edit(embed=ignored_embed, view=None)
        await interaction.response.send_message(f"Novel `{self.novel_title}` ignored successfully.", ephemeral=True)

    async def sweep_duplicates_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("You need `Manage Messages` to execute sweep.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        sorted_items = sorted(
            self.items, 
            key=lambda it: (it.chapter_num, it.created_at.timestamp()), 
            reverse=True
        )

        keep_item = sorted_items[0]
        to_delete = sorted_items[1:]

        deleted_count = 0
        protected_skipped = 0
        failed_count = 0

        for item in to_delete:
            if item.is_protected:
                protected_skipped += 1
                continue

            channel = interaction.guild.get_channel(item.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(item.message_id)
                    await msg.delete()
                    deleted_count += 1
                except Exception:
                    failed_count += 1

        ch_info = f" (Chapter {keep_item.chapter_num:g})" if keep_item.chapter_num != -1.0 else ""
        desc = f"Smart sweep finished for **{self.novel_title}**!\n• Kept newest/latest link{ch_info} in <#{keep_item.channel_id}>.\n• Deleted **{deleted_count}** older duplicates."
        if protected_skipped:
            desc += f"\n• 🛡️ Preserved **{protected_skipped}** links inside protected categories."

        finish_embed = discord.Embed(
            title=f"🧹 Smart Sweep Complete: {self.novel_title}",
            description=desc,
            color=COLOR_SUCCESS
        )

        # Delete the duplicate alert embed entirely from the alert channel
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        await interaction.followup.send(embed=finish_embed, ephemeral=True)

class DeleteButton(discord.ui.Button):
    def __init__(self, item: NovelItem, display_num: int, parent_view: PaginatedNovelClusterView, row: int):
        self.item = item
        self.display_num = display_num
        self.parent_view = parent_view

        super().__init__(
            label=f"Delete {display_num}" if not item.is_protected else f"🛡️ Protected {display_num}",
            style=discord.ButtonStyle.danger if not item.is_protected else discord.ButtonStyle.secondary,
            disabled=item.is_protected,
            custom_id=f"btn_del_msg_{item.message_id}_{display_num}",
            row=row
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Missing `Manage Messages` permission.", ephemeral=True)

        if self.item.is_protected:
            return await interaction.response.send_message(
                "🛡️ This link is inside 'Completed' or 'Long novels' and is protected from deletion.", 
                ephemeral=True
            )

        channel = interaction.guild.get_channel(self.item.channel_id)
        if not channel:
            return await interaction.response.send_message("Channel no longer exists.", ephemeral=True)

        try:
            target_msg = await channel.fetch_message(self.item.message_id)
            await target_msg.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            return await interaction.response.send_message("Missing bot permissions to delete message.", ephemeral=True)
        except Exception as e:
            return await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        # Delete the bot alert embed after single link deletion
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            f"✅ Deleted post #{self.display_num} from <#{self.item.channel_id}> and removed alert embed.",
            ephemeral=True
        )

class NovelScanner(commands.Cog, name="Novel Scanner"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_scanning = False
        self.alert_channel_id = int(os.getenv("ALERT_CHANNEL_ID", "0"))
        # Starts the daily schedule at 5:30 AM IST (00:00 UTC)
        self.scheduled_scan_cycle.start()

    def cog_unload(self):
        self.scheduled_scan_cycle.cancel()

    def get_alert_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        if self.alert_channel_id:
            channel = guild.get_channel(self.alert_channel_id)
            if channel and channel.permissions_for(guild.me).send_messages:
                return channel

        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel

        return next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)

    @tasks.loop(time=DAILY_SCAN_TIME_UTC)
    async def scheduled_scan_cycle(self):
        """Runs daily at 5:30 AM IST (00:00 UTC) only for authorized servers."""
        for guild in self.bot.guilds:
            if not is_guild_allowed(guild.id):
                continue
            alert_channel = self.get_alert_channel(guild)
            if alert_channel:
                await self.execute_server_scan(guild, output_channel=alert_channel, runner=self.bot.user)

    @scheduled_scan_cycle.before_loop
    async def before_scheduled_scan(self):
        await self.bot.wait_until_ready()

    def render_progress_bar(self, percentage: float, length: int = 14) -> str:
        filled = int(round(length * percentage))
        return "█" * filled + "░" * (length - filled)

    async def execute_server_scan(
        self, 
        guild: discord.Guild, 
        output_channel: discord.TextChannel, 
        runner: discord.User | discord.Member,
        target_slug_filter: str | None = None
    ):
        if not is_guild_allowed(guild.id):
            return

        if self.is_scanning:
            if target_slug_filter is None:
                warning_embed = discord.Embed(
                    title="⚠️ Scan Active",
                    description="A scan routine is already executing. Please wait for completion.",
                    color=COLOR_WARNING
                )
                await output_channel.send(embed=warning_embed)
            return

        self.is_scanning = True
        t_start = time.time()
        ignored_slugs = await get_ignored_slugs(guild.id)

        if target_slug_filter and any(are_novels_matching(target_slug_filter, ig) for ig in ignored_slugs):
            self.is_scanning = False
            return

        eligible_channels = [
            c for c in guild.text_channels
            if c.permissions_for(guild.me).read_message_history and c.permissions_for(guild.me).read_messages
        ]

        total_channels = len(eligible_channels)
        messages_inspected = 0
        links_located = 0

        tracker_embed = discord.Embed(
            title="🔍 Scanning Server For Novel Duplicates",
            description=(
                f"**Target:** {guild.name}\n"
                + (f"**Filter:** `{slug_to_display_title(target_slug_filter)}`\n" if target_slug_filter else "")
                + f"**Channels:** {total_channels} detected with readable history.\n"
                "────────────────────────────────────────────"
            ),
            color=COLOR_PRIMARY,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        tracker_embed.add_field(name="Current Channel", value="Initializing...", inline=False)
        tracker_embed.add_field(name="Channels Inspected", value=f"0 / {total_channels}", inline=True)
        tracker_embed.add_field(name="Messages Read", value="0", inline=True)
        tracker_embed.add_field(name="Novel Links", value="0", inline=True)
        tracker_embed.add_field(name="Progress", value=f"`[{self.render_progress_bar(0.0)}]` 0%", inline=False)
        tracker_embed.set_footer(text=f"Initiated by {runner}", icon_url=runner.display_avatar.url)

        dashboard_message = await output_channel.send(embed=tracker_embed)
        last_refresh_time = time.time()
        scanned_entries: list[tuple[str, NovelItem]] = []
        batch_to_cache: list[tuple[int, int, int]] = []

        try:
            for idx, channel in enumerate(eligible_channels, start=1):
                await asyncio.sleep(0.01)

                curr_time = time.time()
                if curr_time - last_refresh_time > 2.5 or idx == total_channels:
                    last_refresh_time = curr_time
                    ratio = idx / total_channels
                    tracker_embed.set_field_at(0, name="Current Channel", value=f"<#{channel.id}> (`#{channel.name}`)", inline=False)
                    tracker_embed.set_field_at(1, name="Channels Inspected", value=f"{idx} / {total_channels}", inline=True)
                    tracker_embed.set_field_at(2, name="Messages Read", value=f"{messages_inspected:,}", inline=True)
                    tracker_embed.set_field_at(3, name="Novel Links", value=f"{links_located:,}", inline=True)
                    tracker_embed.set_field_at(4, name="Progress", value=f"`[{self.render_progress_bar(ratio)}]` {int(ratio * 100)}%", inline=False)
                    try:
                        await dashboard_message.edit(embed=tracker_embed)
                    except discord.HTTPException:
                        pass

                try:
                    async for message in channel.history(limit=800):
                        if message.author.bot:
                            continue

                        messages_inspected += 1
                        urls = URL_REGEX.findall(message.content)
                        if not urls:
                            continue

                        for url in urls:
                            if not is_valid_novel_url(url):
                                continue

                            slug = extract_novel_slug(url)
                            if not slug:
                                continue

                            if any(are_novels_matching(slug, ig) for ig in ignored_slugs):
                                continue

                            if is_channel_named_after_novel(channel.name, slug):
                                continue

                            if target_slug_filter and not are_novels_matching(target_slug_filter, slug):
                                continue

                            links_located += 1
                            item = NovelItem(url=url, message=message)
                            scanned_entries.append((slug, item))
                            batch_to_cache.append((message.id, channel.id, guild.id))

                except (discord.Forbidden, discord.HTTPException):
                    continue

            await mark_messages_scanned(batch_to_cache)
            await update_scan_timestamp(guild.id, time.time())

            # Group matches
            clusters: dict[str, list[NovelItem]] = {}
            for i, (slug, item) in enumerate(scanned_entries):
                if i % 100 == 0:
                    await asyncio.sleep(0)

                found_match = False
                for canonical in list(clusters.keys()):
                    if are_novels_matching(canonical, slug):
                        clusters[canonical].append(item)
                        found_match = True
                        break

                if not found_match:
                    clusters[slug] = [item]

            duplicates = {k: v for k, v in clusters.items() if len(v) >= 2}
            elapsed = round(time.time() - t_start, 2)

            final_summary = discord.Embed(
                title="✅ Server Novel Scan Complete",
                description=f"Full audit finished in **{elapsed}s**.\n────────────────────────────────────────────",
                color=COLOR_SUCCESS,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            final_summary.add_field(name="Channels Inspected", value=f"{total_channels}", inline=True)
            final_summary.add_field(name="Messages Read", value=f"{messages_inspected:,}", inline=True)
            final_summary.add_field(name="Novel Links", value=f"{links_located:,}", inline=True)
            final_summary.add_field(name="Duplicates Found", value=f"**{len(duplicates)}** series", inline=True)
            final_summary.set_footer(text=f"Initiated by {runner}", icon_url=runner.display_avatar.url)
            await dashboard_message.edit(embed=final_summary)

            if not duplicates:
                if target_slug_filter is None:
                    clean_embed = discord.Embed(
                        title="🎉 Zero Duplicates Detected",
                        description="No duplicate novel links were discovered outside of dedicated channels.",
                        color=COLOR_SUCCESS
                    )
                    await output_channel.send(embed=clean_embed)
                return

            for canonical_slug, records in duplicates.items():
                title = slug_to_display_title(canonical_slug)
                view = PaginatedNovelClusterView(
                    canonical_slug=canonical_slug,
                    novel_title=title,
                    items=records,
                    author_id=runner.id,
                    guild_id=guild.id
                )
                await output_channel.send(embed=view.create_embed(), view=view)
                await asyncio.sleep(0.5)

        finally:
            self.is_scanning = False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not is_guild_allowed(message.guild.id):
            return

        urls = URL_REGEX.findall(message.content)
        if not urls:
            return

        for u in urls:
            if not is_valid_novel_url(u):
                continue

            slug = extract_novel_slug(u)
            if not slug:
                continue

            ignored_slugs = await get_ignored_slugs(message.guild.id)
            if any(are_novels_matching(slug, ig) for ig in ignored_slugs):
                continue

            if is_channel_named_after_novel(message.channel.name, slug):
                continue

            alert_channel = self.get_alert_channel(message.guild)
            if alert_channel:
                await self.execute_server_scan(
                    guild=message.guild,
                    output_channel=alert_channel,
                    runner=message.author,
                    target_slug_filter=slug
                )
            break

    @commands.command(name="unignore")
    @commands.has_permissions(manage_messages=True)
    async def unignore_command(self, ctx: commands.Context, *, novel_name_or_slug: str):
        """Removes a novel from the ignore list to resume scanning it."""
        if not is_guild_allowed(ctx.guild.id):
            return

        slug = extract_novel_slug(novel_name_or_slug) or novel_name_or_slug.strip().lower().replace(" ", "-")
        removed = await unignore_novel(ctx.guild.id, slug)
        if removed:
            await ctx.reply(f"✅ Novel `{slug}` has been removed from the ignore list.", mention_author=False)
        else:
            await ctx.reply(f"ℹ️ Novel `{slug}` was not found in the ignore list.", mention_author=False)

    @commands.command(name="novelscan")
    @commands.has_permissions(manage_messages=True)
    async def novelscan(self, ctx: commands.Context):
        """Triggers on-demand server scan. Directs output to the alert channel."""
        if not is_guild_allowed(ctx.guild.id):
            return await ctx.reply("🚫 This bot command is not enabled for this server.", mention_author=False)

        alert_ch = self.get_alert_channel(ctx.guild)
        if not alert_ch:
            alert_ch = ctx.channel

        if ctx.channel.id != alert_ch.id:
            confirm = await ctx.reply(f"🚀 Scan initiated! Monitoring dashboard has been sent to {alert_ch.mention}.", mention_author=False)
            await confirm.delete(delay=5)

        await self.execute_server_scan(ctx.guild, output_channel=alert_ch, runner=ctx.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(NovelScanner(bot))