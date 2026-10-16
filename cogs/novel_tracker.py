from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord.ext import commands, tasks

from novelbot.embeds import (
    Colors,
    error_embed,
    info_embed,
    novel_embed,
    novel_title,
    success_embed,
    trunc,
)
from novelbot.service import get_service
from novelbot.views import BrowserView, build_link_buttons

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


SCAN_INTERVAL_HOURS = _env_int("SCAN_INTERVAL_HOURS", 24, 1, 168)
ALERT_CHANNEL_ID = _env_int("ALERT_CHANNEL_ID", 0, 0, 2**63 - 1)
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:'\""

TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "share", "si", "source", "spm",
}

# Regex to catch path prefixes like /book/slug, /novel/slug, /series/slug, /read/slug
NOVEL_SLUG_PATTERN = re.compile(
    r"^/(?:book|novel|series|read)/([a-zA-Z0-9_\-]+)", re.IGNORECASE
)


def _clean_query(query: str) -> str:
    """Drop tracking parameters but keep identifiers."""
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlencode(sorted(pairs))


def extract_novel_identity(raw_url: str) -> tuple[str, str, str] | None:
    """
    Extract novel identity based on the novel slug, while retaining the full clean URL.
    Returns: (novel_slug, identity_key, full_clean_url)
    """
    try:
        cleaned_raw = raw_url.strip().rstrip(TRAILING_PUNCTUATION)
        parsed = urlsplit(cleaned_raw)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.hostname or parsed.username or parsed.password:
            return None

        hostname = parsed.hostname.lower().rstrip(".").removeprefix("www.")
        port = parsed.port
        if (parsed.scheme.lower(), port) in {("https", 443), ("http", 80)}:
            port = None
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            netloc = f"{netloc}:{port}"

        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")

        query = _clean_query(parsed.query)
        full_clean_url = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))

        # Find the novel slug
        match = NOVEL_SLUG_PATTERN.match(path)
        if match:
            slug = match.group(1).lower()
        else:
            # Fallback for sites where slug is directly off root: e.g. /novel-slug/chapter-1
            parts = [p for p in path.strip("/").split("/") if p]
            slug = parts[0].lower() if parts else ""

        if not slug:
            return None

        # Identity key based on host and novel slug so all chapter links match the same novel
        identity_key = f"{hostname}:{slug}"
        return slug, identity_key, full_clean_url
    except (ValueError, UnicodeError):
        return None


def extract_novels(content: str) -> list[tuple[str, str, str]]:
    """Extracts unique novel links found in message content."""
    found: dict[str, tuple[str, str, str]] = {}
    for match in URL_PATTERN.finditer(content or ""):
        extracted = extract_novel_identity(match.group(0))
        if extracted:
            slug, identity_key, full_url = extracted
            found[identity_key] = (slug, identity_key, full_url)
    return list(found.values())


def is_dedicated_novel_channel(channel_name: str, novel_slug: str) -> bool:
    """Check if the channel name matches the novel name/slug."""
    def clean(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    clean_channel = clean(channel_name)
    clean_slug = clean(novel_slug)
    if not clean_channel or not clean_slug:
        return False
    return clean_slug in clean_channel or clean_channel in clean_slug


def get_alert_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """Configured alert channel for a guild."""
    channel_id = ALERT_CHANNEL_ID
    if not channel_id:
        configured = os.getenv(f"ALERT_CHANNEL_ID_{guild.id}", "").strip()
        if not configured:
            return None
        try:
            channel_id = int(configured)
        except ValueError:
            logger.warning("Invalid alert channel ID for guild %s", guild.id)
            return None
    channel = guild.get_channel(channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None


class DuplicateActionView(discord.ui.View):
    """Provides navigation links and an interactive deletion button for reposts."""

    def __init__(self, target_message: discord.Message, previous_jump: str, full_url: str):
        super().__init__(timeout=86400)
        self.target_message = target_message

        # Direct link button to open the actual novel chapter link
        self.add_item(
            discord.ui.Button(
                label="Open Link",
                url=full_url,
                style=discord.ButtonStyle.link,
                row=0,
            )
        )
        # Jump buttons
        self.add_item(
            discord.ui.Button(
                label="Previous Post",
                url=previous_jump,
                style=discord.ButtonStyle.link,
                row=0,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="New Post",
                url=target_message.jump_url,
                style=discord.ButtonStyle.link,
                row=0,
            )
        )

    @discord.ui.button(
        label="Delete Duplicate",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        row=1,
    )
    async def delete_message_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        channel_perms = (
            self.target_message.channel.permissions_for(interaction.user)
            if isinstance(self.target_message.channel, (discord.TextChannel, discord.Thread))
            else None
        )
        is_author = interaction.user.id == self.target_message.author.id
        can_manage = channel_perms.manage_messages if channel_perms else False

        if not (is_author or can_manage):
            await interaction.response.send_message(
                "You do not have permission to delete this message.", ephemeral=True
            )
            return

        try:
            await self.target_message.delete()
            button.disabled = True
            button.label = "Duplicate Deleted"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("Duplicate message removed.", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message("Message was already deleted.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot lacks permissions to delete the message in that channel.", ephemeral=True
            )


class NovelTracker(commands.Cog):
    """Index novel links, find repeats, and search the server's index."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = get_service(bot)
        self.database = self.service.database
        self._scan_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._closing = False
        self.scan_guilds.change_interval(hours=SCAN_INTERVAL_HOURS)

    async def cog_load(self) -> None:
        await self.database.initialize()
        self.scan_guilds.start()

    async def cog_unload(self) -> None:
        self._closing = True
        self.scan_guilds.cancel()

    # ------------------------------------------------------------ live checking

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.content:
            return
        await self._process_message(message, send_duplicate_alert=True)

    async def _process_message(
        self,
        message: discord.Message,
        *,
        send_duplicate_alert: bool = False,
        duplicates: list[tuple[str, str, str]] | None = None,
    ) -> int:
        if message.guild is None:
            return 0
        novels = extract_novels(message.content)
        if not novels:
            return 0

        try:
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return 0
        except Exception:
            logger.debug("Could not build command context", exc_info=True)

        await self.database.initialize()
        new_links = 0

        for slug, identity, full_url in novels:
            # If posted in a channel dedicated to this novel, ignore duplicate rules
            if hasattr(message.channel, "name") and is_dedicated_novel_channel(
                message.channel.name, slug
            ):
                continue

            try:
                existing = await self.database.find_novel(
                    guild_id=message.guild.id,
                    identity=identity,
                )
                inserted = await self.database.upsert_novel(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    author_id=message.author.id,
                    identity=identity,
                    url=full_url,
                )
                if inserted:
                    new_links += 1

                existing_id = int(existing.get("message_id") or 0) if existing else 0
                if inserted and existing is not None and existing_id != message.id:
                    # Ignore if the older post was originally from that novel's favorite channel
                    old_channel_id = int(existing.get("channel_id") or 0)
                    old_channel = message.guild.get_channel(old_channel_id)
                    if (
                        old_channel
                        and hasattr(old_channel, "name")
                        and is_dedicated_novel_channel(old_channel.name, slug)
                    ):
                        continue

                    _, existing_jump = self._previous_link(message.guild.id, existing)

                    if existing_id < message.id:
                        if duplicates is not None:
                            duplicates.append((full_url, message.jump_url, existing_jump))
                        if send_duplicate_alert:
                            await self._send_duplicate_alert(
                                message, full_url, existing, slug
                            )
                    elif duplicates is not None:
                        duplicates.append((full_url, existing_jump, message.jump_url))
            except Exception:
                logger.exception(
                    "Failed to index novel link %s in guild %s", identity, message.guild.id
                )
        return new_links

    def _get_alert_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        return get_alert_channel(guild)

    @staticmethod
    def _previous_link(guild_id: int, existing: dict) -> tuple[str, str]:
        channel_id = int(existing.get("channel_id") or 0)
        message_id = int(existing.get("message_id") or 0)
        return (
            f"<#{channel_id}>",
            f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
        )

    async def _send_duplicate_alert(
        self,
        message: discord.Message,
        full_url: str,
        existing: dict,
        slug: str,
    ) -> None:
        guild = message.guild
        if guild is None:
            return

        previous_channel, previous_jump = self._previous_link(guild.id, existing)
        identity = str(existing.get("identity") or "")
        meta = (await self.database.get_meta(identity)) if identity else None

        title_display = slug.replace("-", " ").title()

        embed = discord.Embed(
            title=f"🔁 Duplicate Novel Link: {title_display}",
            description=(
                f"**Exact Message Link:**\n{full_url}\n\n"
                "This novel was already shared in another channel."
            ),
            color=Colors.WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🆕 New Post",
            value=f"{message.channel.mention}\n[Jump to message]({message.jump_url})",
            inline=True,
        )
        embed.add_field(
            name="📌 Previous Post",
            value=f"{previous_channel}\n[Jump to message]({previous_jump})",
            inline=True,
        )
        embed.add_field(name="👤 Reposted by", value=message.author.mention, inline=True)

        view = DuplicateActionView(
            target_message=message, previous_jump=previous_jump, full_url=full_url
        )

        alert_channel = self._get_alert_channel(guild)
        try:
            if alert_channel is not None:
                await alert_channel.send(
                    embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await message.reply(
                    embed=embed,
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.Forbidden:
            logger.warning("Cannot send duplicate alerts in guild %s", guild.id)
        except discord.HTTPException:
            logger.exception("Failed to send duplicate alert")

    # ------------------------------------------------------------ scheduled scan

    @tasks.loop(hours=24)
    async def scan_guilds(self) -> None:
        if self._closing:
            return
        for guild in list(self.bot.guilds):
            if self._closing:
                return
            try:
                await self.scan_guild(guild, report=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled scan failed for guild %s", guild.id)

    @scan_guilds.before_loop
    async def before_scan_guilds(self) -> None:
        await self.bot.wait_until_ready()

    async def scan_guild(
        self, guild: discord.Guild, *, report: bool = False
    ) -> dict[str, int]:
        """Scan readable text channels and threads, ignoring novel-dedicated channels."""
        stats = {"channels": 0, "messages": 0, "new_links": 0, "duplicates": 0}
        async with self._scan_locks[guild.id]:
            await self.database.initialize()
            bot_member = guild.me
            if bot_member is None:
                return stats
            duplicates: list[tuple[str, str, str]] = []

            sources = [*guild.text_channels, *guild.threads]
            for channel in sources:
                if self._closing:
                    break
                permissions = channel.permissions_for(bot_member)
                if not (permissions.view_channel and permissions.read_message_history):
                    continue
                try:
                    checkpoint = await self.database.get_checkpoint(
                        guild_id=guild.id,
                        channel_id=channel.id,
                    )
                    after = discord.Object(id=checkpoint) if checkpoint else None
                    latest_message_id = checkpoint or 0
                    processed = 0
                    async for message in channel.history(
                        limit=None,
                        after=after,
                        oldest_first=True,
                    ):
                        latest_message_id = max(latest_message_id, message.id)
                        if not message.author.bot and message.content:
                            stats["new_links"] += await self._process_message(
                                message,
                                send_duplicate_alert=False,
                                duplicates=duplicates,
                            )
                        processed += 1
                        if processed % 200 == 0:
                            await self.database.set_checkpoint(
                                guild_id=guild.id,
                                channel_id=channel.id,
                                message_id=latest_message_id,
                            )
                            await asyncio.sleep(0)
                    if latest_message_id:
                        await self.database.set_checkpoint(
                            guild_id=guild.id,
                            channel_id=channel.id,
                            message_id=latest_message_id,
                        )
                    stats["channels"] += 1
                    stats["messages"] += processed
                except discord.Forbidden:
                    logger.warning("Cannot scan channel %s in guild %s", channel.id, guild.id)
                except discord.HTTPException:
                    logger.exception("Discord API error scanning channel %s", channel.id)

            stats["duplicates"] = len(duplicates)
            logger.info("Scan of guild %s finished: %s", guild.id, stats)

        if report and duplicates:
            await self._send_scan_report(guild, duplicates)
        return stats

    async def _send_scan_report(
        self,
        guild: discord.Guild,
        duplicates: list[tuple[str, str, str]],
    ) -> None:
        channel = self._get_alert_channel(guild)
        if channel is None:
            return
        lines = []
        for url, newer_jump, first_jump in duplicates[:15]:
            lines.append(f"[Link]({url}) — [repeat]({newer_jump}) / [first]({first_jump})")
        description = "\n".join(lines)
        if len(duplicates) > 15:
            description += f"\n...and {len(duplicates) - 15} more. Use `duplicates` to list them."
        embed = discord.Embed(
            title=f"🔁 Scan found {len(duplicates)} duplicate link(s)",
            description=description[:4000],
            color=Colors.WARNING,
            timestamp=discord.utils.utcnow(),
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.exception("Failed to send scan report")

    # ------------------------------------------------------------------ commands

    @commands.command(name="novelscan", aliases=["scan"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 60, commands.BucketType.guild)
    async def manual_scan(self, ctx: commands.Context) -> None:
        """Run a scan of the whole server now."""
        if ctx.guild is None:
            return
        if self._scan_locks[ctx.guild.id].locked():
            await ctx.send(embed=info_embed("A scan is already running for this server."))
            return
        status = await ctx.send(
            embed=info_embed(
                f"Scanning accessible text channels in **{ctx.guild.name}**…",
                title="🔎 Scan started",
            )
        )
        try:
            stats = await self.scan_guild(ctx.guild, report=True)
            embed = success_embed(
                "Indexing finished.",
                title="Scan complete",
            )
            embed.add_field(name="📨 Messages read", value=f"{stats['messages']:,}", inline=True)
            embed.add_field(name="📂 Channels", value=f"{stats['channels']}", inline=True)
            embed.add_field(name="🔗 Links indexed", value=f"{stats['new_links']:,}", inline=True)
            embed.add_field(name="🔁 Duplicates found", value=f"{stats['duplicates']}", inline=True)
            await status.edit(embed=embed)
        except Exception:
            logger.exception("Manual scan failed for guild %s", ctx.guild.id)
            await status.edit(
                embed=error_embed("The scan failed. Check the bot logs for details.", title="Scan failed")
            )

    @commands.command(name="duplicates", aliases=["dupes"])
    @commands.guild_only()
    async def list_duplicates(self, ctx: commands.Context) -> None:
        """Browse the most repeated novel links in this server."""
        if ctx.guild is None:
            return
        try:
            rows = await self.database.find_duplicates(guild_id=ctx.guild.id, limit=100)
            entries = await self.service.decorate(rows)
        except Exception:
            logger.exception("Duplicate lookup failed for guild %s", ctx.guild.id)
            await ctx.send(embed=error_embed("Could not load duplicates. Please try again later."))
            return
        if not entries:
            await ctx.send(embed=info_embed("No duplicate novel links have been found.", title="🔁 Duplicates"))
            return
        for entry in entries:
            entry["badge"] = f"🔁 posted {entry['count']}×"
        view = BrowserView(
            self.service,
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            entries=entries,
            title="🔁 Duplicate Novel Links",
            color=Colors.LIBRARY,
            intro="Links that were shared more than once, most repeated first.",
        )
        await view.send(ctx.send)

    @commands.command(name="novelsearch", aliases=["searchnovel", "findnovel"])
    @commands.guild_only()
    async def search_novels(self, ctx: commands.Context, *, query: str) -> None:
        """Search indexed novel links by title or URL fragment."""
        if ctx.guild is None:
            return
        query = query.strip()
        if not query:
            await ctx.send(embed=error_embed("Enter a title or URL fragment to search for."))
            return
        if len(query) > 100:
            await ctx.send(embed=error_embed("Search terms must be 100 characters or fewer."))
            return
        try:
            results = await self.database.search_novels(
                guild_id=ctx.guild.id,
                query=query,
                limit=100,
            )
            entries = await self.service.decorate(results)
        except Exception:
            logger.exception("Novel search failed for guild %s", ctx.guild.id)
            await ctx.send(embed=error_embed("Search failed. Please try again later."))
            return
        if not entries:
            await ctx.send(
                embed=info_embed(
                    f"Nothing in the index matches **{trunc(query, 80)}**.",
                    title="🔎 No results",
                )
            )
            return
        view = BrowserView(
            self.service,
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            entries=entries,
            title=f"🔎 Results for “{trunc(query, 60)}”",
            color=Colors.INFO,
            intro="Pick a novel from the menu to see its card, track it, or open its links.",
        )
        await view.send(ctx.send)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NovelTracker(bot))