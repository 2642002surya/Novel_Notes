"""Shared service: database + metadata fetching, one instance per bot."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from discord.ext import commands

from cogs.novel_database import NovelDatabase

from .embeds import novel_title
from .fetcher import PageFetcher
from .parser import parse_page

logger = logging.getLogger("NovelGuard.Service")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


DATABASE_NAME = os.getenv("DATABASE_NAME", "novel_index.db").strip() or "novel_index.db"
# How long cached metadata is considered fresh when a user opens a novel card.
METADATA_CACHE_HOURS = _env_int("METADATA_CACHE_HOURS", 6, 1, 168)
FAILURE_RETRY_SECONDS = 3600

# Titles served by bot-protection pages; the data on them is not about the novel.
_BLOCK_PAGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "checking your browser",
    "are you a robot",
    "403 forbidden",
    "pardon our interruption",
)


class NovelService:
    def __init__(self) -> None:
        self.database = NovelDatabase(DATABASE_NAME)
        self.fetcher = PageFetcher()
        self._semaphore = asyncio.Semaphore(3)
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        await self.fetcher.close()

    # ------------------------------------------------------------------ metadata

    async def get_meta(
        self, identity: str, url: str | None, *, force: bool = False
    ) -> dict[str, Any] | None:
        """Cached metadata, refreshed over the network when stale. Never raises."""
        try:
            cached = await self.database.get_meta(identity)
            if not force and cached:
                age = time.time() - float(cached.get("fetched_at") or 0)
                failed = int(cached.get("fail_count") or 0) > 0 and not cached.get("title")
                limit = FAILURE_RETRY_SECONDS if failed else METADATA_CACHE_HOURS * 3600
                if age < limit:
                    return cached
            if not url:
                return cached
            row, _changed = await self.refresh(identity, url)
            return row or cached
        except Exception:
            logger.exception("Metadata lookup failed for %s", identity)
            return None

    async def refresh(
        self, identity: str, url: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """Fetch + parse + store. Returns (row, latest_chapter_changed). Never raises."""
        lock = self._locks.setdefault(identity, asyncio.Lock())
        async with lock:
            fields: dict[str, Any] = {}
            failed = False
            try:
                async with self._semaphore:
                    final_url, html = await self.fetcher.fetch_html(url)
                parsed = parse_page(html, final_url)
                title = (parsed.title or "").lower()
                if parsed.is_empty() or any(title.startswith(t) for t in _BLOCK_PAGE_TITLES):
                    failed = True
                else:
                    fields = {
                        "title": parsed.title,
                        "author": parsed.author,
                        "status": parsed.status,
                        "description": parsed.description,
                        "cover_url": parsed.cover_url,
                        "site_name": parsed.site_name,
                        "icon_url": parsed.icon_url,
                        "latest_chapter": parsed.latest_chapter,
                        "latest_chapter_url": parsed.latest_chapter_url,
                        "released_at": parsed.released_at.isoformat() if parsed.released_at else None,
                    }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                logger.info("Could not fetch %s: %s", url, exc)
            try:
                return await self.database.save_meta(
                    identity=identity, url=url, fields=fields, failed=failed
                )
            except Exception:
                logger.exception("Could not store metadata for %s", identity)
                return None, False

    # ------------------------------------------------------------------- entries

    async def decorate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach cached metadata and a display title to DB rows (no network)."""
        metas = await self.database.get_meta_many([r["identity"] for r in rows if r.get("identity")])
        entries = []
        for row in rows:
            entry = dict(row)
            entry["url"] = entry.get("url") or entry.get("full_url")
            entry["meta"] = metas.get(entry.get("identity"))
            entry["title"] = novel_title(entry, entry["meta"])
            entries.append(entry)
        return entries


def get_service(bot: commands.Bot) -> NovelService:
    service = getattr(bot, "novel_service", None)
    if service is None:
        service = NovelService()
        bot.novel_service = service  # type: ignore[attr-defined]
    return service
