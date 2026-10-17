import asyncio
import datetime
import logging
import time
import discord
from discord.ext import commands

from core.database import upsert_novel_and_source, DB_PATH
from core.utils import (
    URL_REGEX, extract_target_domain, classify_novel_url,
    slug_to_display_title, is_source_whitelisted,
    determine_source_classification
)
from core.builder import ServerBuilder

logger = logging.getLogger("NovelArchiver")

COLOR_PRIMARY = discord.Color.from_rgb(88, 101, 242)
COLOR_SUCCESS = discord.Color.from_rgb(87, 242, 135)
COLOR_WARNING = discord.Color.from_rgb(254, 231, 92)
COLOR_DANGER = discord.Color.from_rgb(237, 66, 69)

SOURCE_GUILD_ID = 896762546713919530
DESTINATION_GUILD_ID = 1560935178879373396

class NovelArchiver(commands.Cog, name="Novel Archiver"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.is_archiving = False

    def render_progress_bar(self, percentage: float, length: int = 14) -> str:
        filled = int(round(length * percentage))
        return "█" * filled + "░" * (length - filled)

    async def _execute_scan_logic(self, ctx: commands.Context):
        """Scans strictly whitelisted categories/channels from the old source server."""
        if self.is_archiving:
            return await ctx.reply("⚠️ An archive scan is already running. Please wait for completion.", mention_author=False)

        # Ensure scan is running against the authorized source server
        if ctx.guild.id != SOURCE_GUILD_ID:
            return await ctx.reply(
                f"🚫 **Access Restricted:** Archive scanner can only be initiated inside the source server (`{SOURCE_GUILD_ID}`).",
                mention_author=False
            )

        self.is_archiving = True
        guild = ctx.guild

        # Filter strictly for whitelisted text channels
        whitelisted_channels = [
            c for c in guild.text_channels
            if is_source_whitelisted(c.category.name if c.category else None, c.name)
            and c.permissions_for(guild.me).read_message_history 
            and c.permissions_for(guild.me).read_messages
        ]

        total_channels = len(whitelisted_channels)
        messages_inspected = 0
        links_located = 0
        t_start = time.time()

        tracker_embed = discord.Embed(
            title="🌌 Celestial Archive Scanner Active",
            description=(
                f"**Source Server:** {guild.name} (`{guild.id}`)\n"
                f"**Scope:** Strictly Whitelisted Channels ({total_channels} detected)\n"
                f"**Target Domains:** `novelbin`, `novelarrow`, `novelping`\n"
                "────────────────────────────────────────────"
            ),
            color=COLOR_PRIMARY,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        tracker_embed.add_field(name="Current Channel", value="Initializing...", inline=False)
        tracker_embed.add_field(name="Whitelisted Channels", value=f"0 / {total_channels}", inline=True)
        tracker_embed.add_field(name="Messages Read", value="0", inline=True)
        tracker_embed.add_field(name="Matching Links", value="0", inline=True)
        tracker_embed.add_field(name="Progress", value=f"`[{self.render_progress_bar(0.0)}]` 0%", inline=False)
        tracker_embed.set_footer(text=f"Initiated by {ctx.author}", icon_url=ctx.author.display_avatar.url)

        dashboard_msg = await ctx.reply(embed=tracker_embed, mention_author=False)
        last_refresh = time.time()

        try:
            for idx, channel in enumerate(whitelisted_channels, start=1):
                await asyncio.sleep(0.01)
                cat_name = channel.category.name if channel.category else None
                class_info = determine_source_classification(cat_name, channel.name)

                curr_time = time.time()
                if curr_time - last_refresh > 2.5 or idx == total_channels:
                    last_refresh = curr_time
                    ratio = idx / total_channels
                    tracker_embed.set_field_at(0, name="Current Channel", value=f"<#{channel.id}> (`#{channel.name}`)", inline=False)
                    tracker_embed.set_field_at(1, name="Whitelisted Channels", value=f"{idx} / {total_channels}", inline=True)
                    tracker_embed.set_field_at(2, name="Messages Read", value=f"{messages_inspected:,}", inline=True)
                    tracker_embed.set_field_at(3, name="Matching Links", value=f"{links_located:,}", inline=True)
                    tracker_embed.set_field_at(4, name="Progress", value=f"`[{self.render_progress_bar(ratio)}]` {int(ratio * 100)}%", inline=False)
                    try:
                        await dashboard_msg.edit(embed=tracker_embed)
                    except discord.HTTPException:
                        pass

                try:
                    async for message in channel.history(limit=None):
                        messages_inspected += 1
                        urls = URL_REGEX.findall(message.content)
                        if not urls:
                            continue

                        for url in urls:
                            domain = extract_target_domain(url)
                            if not domain:
                                continue

                            slug, url_type, ch_num = classify_novel_url(url)
                            if not slug:
                                continue

                            links_located += 1
                            title = slug_to_display_title(slug)

                            await upsert_novel_and_source({
                                "slug": slug,
                                "title": title,
                                "url": url,
                                "domain": domain,
                                "url_type": url_type,
                                "chapter_num": ch_num,
                                "guild_id": guild.id,
                                "channel_id": channel.id,
                                "channel_name": channel.name,
                                "category_name": cat_name,
                                "reading_status": class_info["reading_status"],
                                "theme": class_info["theme"],
                                "destination_category": class_info["destination_category"],
                                "is_completed": class_info["is_completed"],
                                "is_favorite": class_info["is_favorite"],
                                "message_id": message.id,
                                "message_url": message.jump_url
                            })
                except (discord.Forbidden, discord.HTTPException):
                    continue

            elapsed = round(time.time() - t_start, 2)
            final_embed = discord.Embed(
                title="✅ Celestial Archive Scan Complete",
                description=(
                    f"Scan finished in **{elapsed}s**.\n"
                    "Only approved whitelisted channels were scanned. Old server remains untouched.\n"
                    "────────────────────────────────────────────"
                ),
                color=COLOR_SUCCESS,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            final_embed.add_field(name="Channels Inspected", value=f"{total_channels}", inline=True)
            final_embed.add_field(name="Messages Read", value=f"{messages_inspected:,}", inline=True)
            final_embed.add_field(name="Novel Links Preserved", value=f"{links_located:,}", inline=True)
            final_embed.set_footer(text="Open Web Dashboard to review classification before setup.")
            await dashboard_msg.edit(embed=final_embed)

        finally:
            self.is_archiving = False

    async def _execute_setup_logic(self, ctx: commands.Context):
        """Provisions destination server layout safely."""
        # Enforce that setup only runs in the designated new server
        if ctx.guild.id != DESTINATION_GUILD_ID:
            return await ctx.reply(
                f"🚫 **Destination Mismatch:** `setup-server` is configured to build in new server ID `{DESTINATION_GUILD_ID}`.",
                mention_author=False
            )

        builder = ServerBuilder(self.bot, ctx.guild)
        audit = await builder.preflight_audit()

        if audit["capacity_exceeded"]:
            return await ctx.reply(
                f"🚫 **Capacity Abort:** New server would exceed Discord's 500 channel limit "
                f"({audit['projected_total_channels']} projected). Please review channel allocation in dashboard.",
                mention_author=False
            )

        confirm_embed = discord.Embed(
            title="⚙️ Celestial Server Builder Preflight",
            description=(
                f"Destination Server: **{ctx.guild.name}** (`{ctx.guild.id}`)\n"
                f"• Approved Unique Novels: **{audit['total_novels_ready']}**\n"
                f"• Existing Server Channels: **{audit['existing_channels']} / 500**\n"
                f"• Channels to Provision: **{audit['planned_channels_to_create']}**\n\n"
                "⚠️ This will construct the Celestial Archive structure additively without deleting existing content.\n"
                "**Reply with `CONFIRM` within 30 seconds to begin provisioning.**"
            ),
            color=COLOR_WARNING
        )
        await ctx.reply(embed=confirm_embed, mention_author=False)

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.strip().upper() == "CONFIRM"

        try:
            await self.bot.wait_for("message", timeout=30.0, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("⏳ Provisioning canceled: Confirmation timed out.")

        progress_msg = await ctx.send("🚀 Starting Celestial Server Builder...")

        async def update_progress(status: str, percent: float):
            try:
                await progress_msg.edit(content=f"🔨 [{int(percent*100)}%] {status}")
            except discord.HTTPException:
                pass

        report = await builder.execute_build(progress_callback=update_progress)

        summary_embed = discord.Embed(
            title="🎉 Celestial Archive Server Provisioned!",
            description=(
                f"• Categories Created: **{report['categories_created']}**\n"
                f"• Channels Created: **{report['channels_created']}**\n"
                f"• Links Posted: **{report['links_posted']}**\n"
            ),
            color=COLOR_SUCCESS
        )
        await progress_msg.edit(content=None, embed=summary_embed)

    # Command entrypoints: Supports scan-server, scan server, scan
    @commands.group(name="scan", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def scan_group(self, ctx: commands.Context):
        await self._execute_scan_logic(ctx)

    @scan_group.command(name="server")
    @commands.has_permissions(administrator=True)
    async def scan_server_subcommand(self, ctx: commands.Context):
        await self._execute_scan_logic(ctx)

    @commands.command(name="scan-server", aliases=["scan_server", "scanserver"])
    @commands.has_permissions(administrator=True)
    async def scan_server_command(self, ctx: commands.Context):
        await self._execute_scan_logic(ctx)

    # Command entrypoints: Supports setup-server, setup server, setup
    @commands.group(name="setup", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def setup_group(self, ctx: commands.Context):
        await self._execute_setup_logic(ctx)

    @setup_group.command(name="server")
    @commands.has_permissions(administrator=True)
    async def setup_server_subcommand(self, ctx: commands.Context):
        await self._execute_setup_logic(ctx)

    @commands.command(name="setup-server", aliases=["setup_server", "setupserver"])
    @commands.has_permissions(administrator=True)
    async def setup_server_command(self, ctx: commands.Context):
        await self._execute_setup_logic(ctx)

async def setup(bot: commands.Bot):
    await bot.add_cog(NovelArchiver(bot))