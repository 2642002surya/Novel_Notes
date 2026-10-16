from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord.ext import commands, tasks

from .novel_database import NovelDatabase

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


DATABASE_NAME = os.getenv("DATABASE_NAME", "novel_index.db").strip() or "novel_index.db"
# Full re-check of the whole server. Default: once every 24 hours.
SCAN_INTERVAL_HOURS = _env_int("SCAN_INTERVAL_HOURS", 24, 1, 168)
ALERT_CHANNEL_ID = _env_int("ALERT_CHANNEL_ID", 0, 0, 2**63 - 1)
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:'\""
# Query parameters that only track clicks and never change which page is shown.
TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "share", "si", "source", "spm",
}


def _clean_query(query: str) -> str:
    """Drop tracking parameters but keep ones that identify the page (?id=5)."""
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    return urlencode(sorted(pairs))


def normalize_novel_url(raw_url: str) -> tuple[str, str] | None:
    """Normalize a web URL for indexing: no fragment, no tracking parameters."""
    try:
        parsed = urlsplit(raw_url.strip().rstrip(TRAILING_PUNCTUATION))
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.hostname or parsed.username or parsed.password:
            return None

        hostname = parsed.hostname.lower().rstrip(".")
        if not hostname:
            return None
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
        cleaned_url = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
        identity = f"{netloc.lower()}{path}" + (f"?{query}" if query else "")
        return identity, cleaned_url
    except (ValueError, UnicodeError):
        return None


def extract_novels(content: str) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for match in URL_PATTERN.finditer(content or ""):
        normalized = normalize_novel_url(match.group(0))
        if normalized:
            identity, cleaned_url = normalized
            found.setdefault(identity, cleaned_url)
    return list(found.items())


class NovelTracker(commands.Cog):
    """Index novel links, find repeats, and search the server's index.

    Behaviour:
      * A full scan of every readable channel runs on startup, then every
        SCAN_INTERVAL_HOURS (default 24).
      * Between scans the bot is idle. Each new message is checked instantly
        against the index and a duplicate alert is sent if the link was
        posted before.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.database = NovelDatabase(DATABASE_NAME)
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
        """Index links in a message. Returns the number of new links indexed.

        `duplicates` collects (url, newer_post_jump_url, first_post_jump_url).
        """
        if message.guild is None:
            return 0
        novels = extract_novels(message.content)
        if not novels:
            return 0

        # A command such as `i novel novelsearch https://...` is not a novel post.
        try:
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                return 0
        except Exception:
            logger.debug("Could not build command context", exc_info=True)

        await self.database.initialize()

        new_links = 0
        for identity, url in novels:
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
                    url=url,
                )
                if inserted:
                    new_links += 1
                existing_id = int(existing.get("message_id") or 0) if existing else 0
                if inserted and existing is not None and existing_id != message.id:
                    _, existing_jump = self._previous_link(message.guild.id, existing)
                    if existing_id < message.id:
                        # Normal case: this message repeats an older one.
                        if duplicates is not None:
                            duplicates.append((url, message.jump_url, existing_jump))
                        if send_duplicate_alert:
                            await self._send_duplicate_alert(message, url, existing)
                    elif duplicates is not None:
                        # Scan reached an older post after a newer one was stored
                        # (channels are scanned one after another).
                        duplicates.append((url, existing_jump, message.jump_url))
            except Exception:
                logger.exception(
                    "Failed to index a novel link in guild %s", message.guild.id
                )
        return new_links

    def _get_alert_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
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
        url: str,
        existing: dict,
    ) -> None:
        guild = message.guild
        if guild is None:
            return

        previous_channel, previous_jump = self._previous_link(guild.id, existing)
        embed = discord.Embed(
            title="Duplicate Novel Link",
            description=f"This novel link was already shared before.\n\n[Open Novel]({url})",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="New Post",
            value=f"{message.channel.mention}\n[Jump to message]({message.jump_url})",
            inline=True,
        )
        embed.add_field(
            name="Previous Post",
            value=f"{previous_channel}\n[Jump to message]({previous_jump})",
            inline=True,
        )

        # Alert channel if configured, otherwise reply right under the new post.
        alert_channel = self._get_alert_channel(guild)
        try:
            if alert_channel is not None:
                await alert_channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                await message.reply(
                    embed=embed,
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
        """Scan every readable text channel. Resumes from per-channel checkpoints,
        so the first run covers the full history and later runs only read what
        is new."""
        stats = {"channels": 0, "messages": 0, "new_links": 0, "duplicates": 0}
        async with self._scan_locks[guild.id]:
            await self.database.initialize()
            bot_member = guild.me
            if bot_member is None:
                return stats
            duplicates: list[tuple[str, str, str]] = []

            # Text channels plus currently active threads / forum posts.
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
                            # Historical messages are collected for one summary
                            # instead of sending an alert per message.
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
                            await asyncio.sleep(0)  # yield to the event loop
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
            title=f"Scan found {len(duplicates)} duplicate link(s)",
            description=description[:4000],
            color=discord.Color.orange(),
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
            await ctx.send("A scan is already running for this server.")
            return
        status = await ctx.send(
            f"Starting a scan of accessible text channels in **{ctx.guild.name}**..."
        )
        try:
            stats = await self.scan_guild(ctx.guild, report=True)
            await status.edit(
                content=(
                    f"Scan completed: **{stats['messages']:,}** new message(s) read in "
                    f"**{stats['channels']}** channel(s), **{stats['new_links']:,}** link(s) "
                    f"indexed, **{stats['duplicates']}** duplicate(s) found."
                )
            )
        except Exception:
            logger.exception("Manual scan failed for guild %s", ctx.guild.id)
            await status.edit(content="The scan failed. Check the bot logs for details.")

    @commands.command(name="duplicates", aliases=["dupes"])
    @commands.guild_only()
    async def list_duplicates(self, ctx: commands.Context) -> None:
        """List the most repeated novel links in this server."""
        if ctx.guild is None:
            return
        try:
            rows = await self.database.find_duplicates(guild_id=ctx.guild.id, limit=15)
        except Exception:
            logger.exception("Duplicate lookup failed for guild %s", ctx.guild.id)
            await ctx.send("Could not load duplicates. Please try again later.")
            return
        if not rows:
            await ctx.send("No duplicate novel links have been found.")
            return
        lines = [
            f"**{row['count']}x** {row['url']} — [first post]"
            f"(https://discord.com/channels/{ctx.guild.id}/{int(row['channel_id'])}/{int(row['message_id'])})"
            for row in rows
        ]
        embed = discord.Embed(
            title="Duplicate Novel Links",
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="novelsearch", aliases=["searchnovel", "findnovel"])
    @commands.guild_only()
    async def search_novels(self, ctx: commands.Context, *, query: str) -> None:
        """Search indexed novel links by title or URL fragment."""
        if ctx.guild is None:
            return
        query = query.strip()
        if not query:
            await ctx.send("Enter a title or URL fragment to search for.")
            return
        if len(query) > 100:
            await ctx.send("Search terms must be 100 characters or fewer.")
            return
        try:
            results = await self.database.search_novels(
                guild_id=ctx.guild.id,
                query=query,
                limit=10,
            )
        except Exception:
            logger.exception("Novel search failed for guild %s", ctx.guild.id)
            await ctx.send("Search failed. Please try again later.")
            return
        if not results:
            await ctx.send("No matching novels were found in the index.")
            return

        embed = discord.Embed(
            title="Novel Search Results",
            description=f"Found `{len(results)}` matching result(s).",
            color=discord.Color.blurple(),
        )
        for result in results[:10]:
            title = str(result.get("clean_title") or result.get("slug") or "Novel")
            url = str(result.get("url") or "")
            value = (
                f"[Open novel]({url})"
                if url.startswith(("https://", "http://"))
                else "No valid URL available."
            )
            channel_id = result.get("channel_id")
            message_id = result.get("message_id")
            if channel_id and message_id:
                value += (
                    f"\n[Original Discord message]"
                    f"(https://discord.com/channels/{ctx.guild.id}/{int(channel_id)}/{int(message_id)})"
                )
            embed.add_field(name=title[:256], value=value[:1024], inline=False)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NovelTracker(bot))
