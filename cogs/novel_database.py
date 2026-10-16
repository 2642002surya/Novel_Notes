from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("NovelGuard.Database")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class NovelDatabase:
    """Async facade over a small, migration-friendly SQLite novel index."""

    def __init__(self, database_name: str = "novel_index.db") -> None:
        path = Path(database_name).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.path = path.resolve()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 20000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await asyncio.to_thread(self._initialize_sync)
                self._initialized = True

    def _initialize_sync(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tracked_novels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    slug TEXT NOT NULL DEFAULT '',
                    clean_title TEXT NOT NULL DEFAULT '',
                    base_url TEXT NOT NULL DEFAULT '',
                    full_url TEXT NOT NULL DEFAULT '',
                    content_preview TEXT,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL DEFAULT 0,
                    jump_url TEXT NOT NULL DEFAULT '',
                    first_posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    identity TEXT
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS channel_checkpoints (
                    channel_id INTEGER PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    guild_id INTEGER
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ignored_novels (
                    guild_id INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    PRIMARY KEY (guild_id, slug)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS blacklisted_channels (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                )"""
            )

            required_novel_columns = {
                "guild_id": "INTEGER NOT NULL DEFAULT 0",
                "slug": "TEXT NOT NULL DEFAULT ''",
                "clean_title": "TEXT NOT NULL DEFAULT ''",
                "base_url": "TEXT NOT NULL DEFAULT ''",
                "full_url": "TEXT NOT NULL DEFAULT ''",
                "content_preview": "TEXT",
                "channel_id": "INTEGER NOT NULL DEFAULT 0",
                "message_id": "INTEGER NOT NULL DEFAULT 0",
                "author_id": "INTEGER NOT NULL DEFAULT 0",
                "jump_url": "TEXT NOT NULL DEFAULT ''",
                "first_posted_at": "TIMESTAMP",
                "identity": "TEXT",
            }
            for column, definition in required_novel_columns.items():
                self._ensure_column(connection, "tracked_novels", column, definition)
            self._ensure_column(connection, "channel_checkpoints", "guild_id", "INTEGER")
            self._ensure_column(
                connection, "channel_checkpoints", "last_message_id", "INTEGER NOT NULL DEFAULT 0"
            )

            connection.execute(
                """UPDATE tracked_novels
                   SET identity = lower(slug)
                   WHERE identity IS NULL OR identity = ''"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_novels_guild_identity ON tracked_novels (guild_id, identity)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_novels_guild_slug ON tracked_novels (guild_id, slug)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_novels_channel_message ON tracked_novels (channel_id, message_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_guild_channel ON channel_checkpoints (guild_id, channel_id)"
            )
            connection.commit()
        finally:
            connection.close()
        logger.info("Database initialized: %s", self.path)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        # Table and column names are constants supplied by this module.
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------ lookups

    async def find_novel(self, *, guild_id: int, identity: str) -> dict[str, Any] | None:
        """Return the earliest recorded post of this link, if any."""
        await self.initialize()
        return await asyncio.to_thread(self._find_novel_sync, guild_id, identity)

    def _find_novel_sync(self, guild_id: int, identity: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT channel_id, message_id, author_id, full_url, jump_url,
                          identity, slug, first_posted_at
                   FROM tracked_novels
                   WHERE guild_id = ? AND identity = ?
                   ORDER BY message_id ASC, id ASC LIMIT 1""",
                (guild_id, identity),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    # ------------------------------------------------------------------ writes

    async def upsert_novel(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        identity: str,
        url: str,
    ) -> bool:
        """Record a posting. Returns True if a new row was inserted."""
        await self.initialize()
        return await asyncio.to_thread(
            self._upsert_novel_sync,
            guild_id,
            channel_id,
            message_id,
            author_id,
            identity,
            url,
        )

    def _upsert_novel_sync(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        identity: str,
        url: str,
    ) -> bool:
        parsed = urlsplit(url)
        host = parsed.netloc or identity.split("/", 1)[0]
        path_parts = [part for part in parsed.path.split("/") if part]
        slug = path_parts[-1] if path_parts else host
        title_source = slug.replace("_", "-").replace("-", " ")
        title = " ".join(word.capitalize() for word in title_source.split()) or slug
        base_url = f"{parsed.scheme}://{host}" if parsed.scheme and host else url
        jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

        connection = self._connect()
        try:
            cursor = connection.execute(
                """INSERT INTO tracked_novels (
                       guild_id, slug, clean_title, base_url, full_url,
                       content_preview, channel_id, message_id, author_id,
                       jump_url, identity
                   )
                   SELECT ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM tracked_novels
                       WHERE guild_id = ? AND channel_id = ?
                         AND message_id = ? AND identity = ?
                   )""",
                (
                    guild_id, slug, title, base_url, url, channel_id,
                    message_id, author_id, jump_url, identity,
                    guild_id, channel_id, message_id, identity,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    # ------------------------------------------------------------- checkpoints

    async def get_checkpoint(self, *, guild_id: int, channel_id: int) -> int | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_checkpoint_sync, guild_id, channel_id)

    def _get_checkpoint_sync(self, guild_id: int, channel_id: int) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT last_message_id FROM channel_checkpoints
                   WHERE channel_id = ? AND (guild_id = ? OR guild_id IS NULL)
                   LIMIT 1""",
                (channel_id, guild_id),
            ).fetchone()
            return int(row["last_message_id"]) if row else None
        finally:
            connection.close()

    async def set_checkpoint(
        self, *, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.initialize()
        await asyncio.to_thread(self._set_checkpoint_sync, guild_id, channel_id, message_id)

    def _set_checkpoint_sync(self, guild_id: int, channel_id: int, message_id: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """INSERT INTO channel_checkpoints (channel_id, last_message_id, guild_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel_id) DO UPDATE SET
                       last_message_id = CASE
                           WHEN excluded.last_message_id > channel_checkpoints.last_message_id
                           THEN excluded.last_message_id
                           ELSE channel_checkpoints.last_message_id
                       END,
                       guild_id = excluded.guild_id""",
                (channel_id, message_id, guild_id),
            )
            connection.commit()
        finally:
            connection.close()

    # ------------------------------------------------------------------ search

    async def search_novels(
        self, *, guild_id: int, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        await self.initialize()
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 25))
        return await asyncio.to_thread(self._search_novels_sync, guild_id, query, limit)

    def _search_novels_sync(
        self, guild_id: int, query: str, limit: int
    ) -> list[dict[str, Any]]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT identity, slug, clean_title, full_url AS url,
                          channel_id, MIN(message_id) AS message_id, first_posted_at
                   FROM tracked_novels
                   WHERE guild_id = ? AND (
                       identity LIKE ? ESCAPE '\\' OR slug LIKE ? ESCAPE '\\'
                       OR clean_title LIKE ? ESCAPE '\\' OR full_url LIKE ? ESCAPE '\\'
                   )
                   GROUP BY identity
                   ORDER BY MIN(message_id) ASC LIMIT ?""",
                (guild_id, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    # -------------------------------------------------------------- duplicates

    async def find_duplicates(
        self, *, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Links posted in more than one message, most-repeated first."""
        await self.initialize()
        limit = max(1, min(int(limit), 50))
        return await asyncio.to_thread(self._find_duplicates_sync, guild_id, limit)

    def _find_duplicates_sync(self, guild_id: int, limit: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT t.identity, t.full_url AS url, t.channel_id, t.message_id,
                          d.cnt AS count
                   FROM tracked_novels t
                   JOIN (
                       SELECT identity, MIN(message_id) AS first_id, COUNT(*) AS cnt
                       FROM tracked_novels
                       WHERE guild_id = ? AND identity != ''
                       GROUP BY identity
                       HAVING COUNT(*) > 1
                   ) d ON t.identity = d.identity AND t.message_id = d.first_id
                   WHERE t.guild_id = ?
                   GROUP BY t.identity
                   ORDER BY d.cnt DESC, t.message_id ASC
                   LIMIT ?""",
                (guild_id, guild_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
