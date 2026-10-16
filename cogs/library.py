from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import discord
from discord.ext import commands, tasks

from novelbot.embeds import (
    Colors,
    derive_title,
    error_embed,
    info_embed,
    novel_title,
    release_embed,
    success_embed,
    trunc,
    untracked_embed,
)
from novelbot.service import get_service
from novelbot.views import (
    BrowserView,
    MatchPickerView,
    MuteButton,
    UnsubscribeButton,
    build_release_view,
)

from .novel_tracker import get_alert_channel, normalize_novel_url

logger = logging.getLogger("NovelGuard.Library")

MENTIONS_PER_MESSAGE = 60  # 60 mentions * ~22 chars stays well under 2000


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# How often subscribed novels are re-checked for new chapters.
RELEASE_CHECK_MINUTES = _env_int("RELEASE_CHECK_MINUTES", 60, 10, 1440)


def _norm(text: object) -> str:
    """Lowercase and treat '-', '_', '/', '.' as spaces so 'brand new' finds 'brand-new-novel'."""
    return " ".join(re.sub(r"[-_/.:?=&]+", " ", str(text or "").lower()).split())


class NovelLibrary(commands.Cog):
    """Track novels, browse your library, and get new-chapter alerts."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = get_service(bot)
        self.database = self.service.database
        self._closing = False
        self._check_lock = asyncio.Lock()
        self.release_check.change_interval(minutes=RELEASE_CHECK_MINUTES)

    async def cog_load(self) -> None:
        await self.database.initialize()
        # Release-alert buttons are persistent: they must work after a restart.
        self.bot.add_dynamic_items(UnsubscribeButton, MuteButton)
        self.release_check.start()

    async def cog_unload(self) -> None:
        self._closing = True
        self.release_check.cancel()
        self.bot.remove_dynamic_items(UnsubscribeButton, MuteButton)

    # --------------------------------------------------------------- lookups

    async def _find_matches(
        self, ctx: commands.Context, query: str
    ) -> list[dict[str, Any]]:
        """Resolve a URL or a title/slug fragment to indexed novels."""
        assert ctx.guild is not None
        query = query.strip()
        normalized = normalize_novel_url(query) if query.lower().startswith(("http://", "https://")) else None
        if normalized:
            identity, url = normalized
            row = await self.database.find_novel(guild_id=ctx.guild.id, identity=identity)
            if row is None:
                # A link the bot hasn't seen yet: index it so it can be tracked.
                await self.database.upsert_novel(
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                    message_id=ctx.message.id,
                    author_id=ctx.author.id,
                    identity=identity,
                    url=url,
                )
                row = await self.database.find_novel(guild_id=ctx.guild.id, identity=identity)
            return await self.service.decorate([row] if row else [])
        rows = await self.database.search_novels(guild_id=ctx.guild.id, query=query, limit=100)
        return await self.service.decorate(rows)

    # -------------------------------------------------------------- commands

    @commands.command(name="track", aliases=["subscribe", "follow"])
    @commands.guild_only()
    async def track(self, ctx: commands.Context, *, query: str) -> None:
        """Track a novel for new-chapter alerts. Use a title fragment or a link."""
        assert ctx.guild is not None
        try:
            matches = await self._find_matches(ctx, query)
        except Exception:
            logger.exception("Track lookup failed")
            await ctx.send(embed=error_embed("Could not search the index. Please try again."))
            return
        if not matches:
            await ctx.send(
                embed=error_embed(
                    f"No novel in the index matches **{trunc(query, 80)}**.\n"
                    "Paste a novel link instead and I'll add it.",
                    title="Novel not found",
                )
            )
            return

        if len(matches) == 1:
            # Exact match: open the card straight away, already tracked.
            entry = matches[0]
            await self.database.subscribe(
                guild_id=ctx.guild.id, user_id=ctx.author.id, identity=entry["identity"]
            )
            view = BrowserView(
                self.service,
                author_id=ctx.author.id,
                guild_id=ctx.guild.id,
                entries=[entry],
                title="Tracked",
                color=Colors.INFO,
                open_identity=entry["identity"],
            )
            async with ctx.typing():  # first lookup may fetch the page
                await view.send(ctx.send, flash="tracked")
            return

        picker = MatchPickerView(
            self.service,
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            entries=matches,
            action="track",
        )
        extra = ""
        if len(matches) > len(picker.entries):
            extra = f"\nShowing the first {len(picker.entries)} of {len(matches)} matches — refine your search to narrow it."
        embed = picker.prompt_embed(query)
        if extra:
            embed.description = (embed.description or "") + extra
        picker.message = await ctx.send(embed=embed, view=picker)

    @commands.command(name="untrack", aliases=["unsubscribe", "unfollow"])
    @commands.guild_only()
    async def untrack(self, ctx: commands.Context, *, query: str) -> None:
        """Stop tracking a novel. Matches against the novels you currently track."""
        assert ctx.guild is not None
        rows = await self.database.list_user_subscriptions(
            guild_id=ctx.guild.id, user_id=ctx.author.id
        )
        entries = await self.service.decorate(rows)
        needle = _norm(query)
        matches = [
            e for e in entries
            if needle in _norm(e.get("title"))
            or needle in _norm(derive_title(e.get("url")))
            or needle in _norm(e.get("identity"))
        ]
        if not matches:
            await ctx.send(
                embed=error_embed(
                    f"You aren't tracking anything that matches **{trunc(query, 80)}**.\n"
                    "Use the `library` command to see what you track.",
                    title="Nothing to untrack",
                )
            )
            return
        if len(matches) == 1:
            entry = matches[0]
            await self.database.unsubscribe(
                guild_id=ctx.guild.id, user_id=ctx.author.id, identity=entry["identity"]
            )
            await ctx.send(embed=untracked_embed(entry["title"], meta=entry.get("meta"), url=entry.get("url")))
            return
        picker = MatchPickerView(
            self.service,
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            entries=matches,
            action="untrack",
        )
        picker.message = await ctx.send(embed=picker.prompt_embed(query), view=picker)

    @commands.command(name="library", aliases=["mylist", "tracked", "shelf"])
    @commands.guild_only()
    async def library(self, ctx: commands.Context, scope: str = "mine") -> None:
        """Browse the novels you track. Use `library server` for everything indexed here."""
        assert ctx.guild is not None
        scope = scope.lower()
        if scope in ("server", "all", "guild"):
            rows = await self.database.list_indexed(guild_id=ctx.guild.id, limit=500)
            entries = await self.service.decorate(rows)
            title, empty = f"📚 {ctx.guild.name} — Server Library", "No novels have been indexed yet."
            intro = "Every novel link shared here, most recent first."
            drop = False
        else:
            rows = await self.database.list_user_subscriptions(
                guild_id=ctx.guild.id, user_id=ctx.author.id
            )
            entries = await self.service.decorate(rows)
            for entry, row in zip(entries, rows):
                entry["muted"] = bool(row.get("muted"))
            title, empty = f"📚 {ctx.author.display_name}'s Library", (
                "You aren't tracking anything yet.\nUse `track <title or link>` or `novelsearch` to get started."
            )
            intro = "🔔 = pings on · 🔕 = muted. Pick a novel to see its card."
            drop = True
        if not entries:
            await ctx.send(embed=info_embed(empty, title=title))
            return
        view = BrowserView(
            self.service,
            author_id=ctx.author.id,
            guild_id=ctx.guild.id,
            entries=entries,
            title=title,
            color=Colors.LIBRARY,
            intro=intro,
            empty_text=empty,
            drop_on_untrack=drop,
        )
        await view.send(ctx.send)

    @commands.command(name="novelinfo", aliases=["noveldetails"])
    @commands.guild_only()
    async def novel_info(self, ctx: commands.Context, *, query: str) -> None:
        """Show the card for one novel (cover, author, status, latest chapter)."""
        assert ctx.guild is not None
        matches = await self._find_matches(ctx, query)
        if not matches:
            await ctx.send(
                embed=error_embed(
                    f"No novel in the index matches **{trunc(query, 80)}**.", title="Novel not found"
                )
            )
            return
        if len(matches) == 1:
            view = BrowserView(
                self.service,
                author_id=ctx.author.id,
                guild_id=ctx.guild.id,
                entries=matches,
                title="Novel",
                color=Colors.INFO,
                open_identity=matches[0]["identity"],
            )
        else:
            view = BrowserView(
                self.service,
                author_id=ctx.author.id,
                guild_id=ctx.guild.id,
                entries=matches,
                title=f"🔎 Matches for “{trunc(query, 60)}”",
                color=Colors.INFO,
                intro="Several novels match — pick one.",
            )
        async with ctx.typing():
            await view.send(ctx.send)

    @commands.command(name="checkreleases", aliases=["checknow"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 120, commands.BucketType.guild)
    async def check_releases(self, ctx: commands.Context) -> None:
        """Check tracked novels for new chapters right now."""
        assert ctx.guild is not None
        status = await ctx.send(embed=info_embed("Checking tracked novels…", title="🔎 Release check"))
        stats = await self.run_release_check(only_guild=ctx.guild.id)
        embed = success_embed("Finished checking.", title="Release check complete")
        embed.add_field(name="📖 Novels checked", value=str(stats["checked"]), inline=True)
        embed.add_field(name="🆕 New chapters", value=str(stats["new"]), inline=True)
        embed.add_field(name="⚠️ Unreachable", value=str(stats["failed"]), inline=True)
        await status.edit(embed=embed)

    # ------------------------------------------------------- release checker

    @tasks.loop(minutes=60)
    async def release_check(self) -> None:
        if self._closing:
            return
        try:
            await self.run_release_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Release check failed")

    @release_check.before_loop
    async def before_release_check(self) -> None:
        await self.bot.wait_until_ready()

    async def run_release_check(self, only_guild: int | None = None) -> dict[str, int]:
        """Re-fetch every subscribed novel once and announce new chapters.

        A refresh consumes the "chapter changed" signal, so a change is always
        announced in *every* guild that tracks the novel, even for a manual run
        that was started from just one of them.
        """
        stats = {"checked": 0, "new": 0, "failed": 0}
        async with self._check_lock:
            pairs = await self.database.list_subscribed_identities()
            by_identity: dict[str, list[dict[str, Any]]] = {}
            for pair in pairs:
                by_identity.setdefault(pair["identity"], []).append(pair)

            for identity, group in by_identity.items():
                if self._closing:
                    break
                if only_guild is not None and all(p["guild_id"] != only_guild for p in group):
                    continue
                url = next((p["url"] for p in group if p.get("url")), None)
                if not url:
                    continue
                row, changed = await self.service.refresh(identity, url)
                stats["checked"] += 1
                if row is None or int(row.get("fail_count") or 0) > 0:
                    stats["failed"] += 1
                if changed and row is not None:
                    stats["new"] += 1
                    for pair in group:
                        await self._announce(pair, row)
                await asyncio.sleep(1)  # be polite to the source sites
        return stats

    async def _announce(self, pair: dict[str, Any], meta: dict[str, Any]) -> None:
        guild = self.bot.get_guild(int(pair["guild_id"]))
        if guild is None:
            return
        identity = pair["identity"]
        subscribers = await self.database.list_subscribers(guild_id=guild.id, identity=identity)
        active = [int(s["user_id"]) for s in subscribers if not s["muted"]]
        if not subscribers:
            return

        channel: Any = get_alert_channel(guild)
        if channel is None and pair.get("channel_id"):
            channel = guild.get_channel_or_thread(int(pair["channel_id"]))
        if channel is None:
            logger.warning("No channel to announce %s in guild %s", identity, guild.id)
            return
        me = guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                logger.warning("Cannot announce in #%s (guild %s): missing permissions", channel, guild.id)
                return

        row = {"url": pair.get("url") or meta.get("url"), "identity": identity}
        embed = release_embed(row=row, meta=meta, subscriber_count=len(subscribers))
        view = build_release_view(
            self.database.identity_hash(identity),
            title=novel_title(row, meta),
            url=row["url"],
            meta=meta,
        )

        chunks = [
            active[i : i + MENTIONS_PER_MESSAGE]
            for i in range(0, len(active), MENTIONS_PER_MESSAGE)
        ] or [[]]
        mentions = discord.AllowedMentions(users=True, roles=False, everyone=False)
        try:
            await channel.send(
                content=" ".join(f"<@{uid}>" for uid in chunks[0]) or None,
                embed=embed,
                view=view,
                allowed_mentions=mentions,
            )
            for extra in chunks[1:]:
                await channel.send(
                    " ".join(f"<@{uid}>" for uid in extra), allowed_mentions=mentions
                )
        except discord.HTTPException:
            logger.exception("Failed to announce release for %s in guild %s", identity, guild.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NovelLibrary(bot))
