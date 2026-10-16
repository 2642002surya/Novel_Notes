from __future__ import annotations

import asyncio
import logging
import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("NovelGuard.Database")

# Links that are media/emoji, not novels. They stay in the index (so duplicate
# detection history is untouched) but are hidden from listings and search.
NOISE_HOSTS = (
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
    "tenor.com",
    "media.tenor.com",
    "giphy.com",
    "media.giphy.com",
)
_NOISE_SQL = " AND ".join(f"identity NOT LIKE '{host}/%'" for host in NOISE_HOSTS)
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

            connection.execute(
                """CREATE TABLE IF NOT EXISTS novel_meta (
                    identity TEXT PRIMARY KEY,
                    identity_hash TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT,
                    author TEXT,
                    status TEXT,
                    description TEXT,
                    cover_url TEXT,
                    site_name TEXT,
                    icon_url TEXT,
                    latest_chapter TEXT,
                    latest_chapter_url TEXT,
                    released_at TEXT,
                    fetched_at REAL NOT NULL DEFAULT 0,
                    changed_at REAL,
                    fail_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_meta_hash ON novel_meta (identity_hash)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS subscriptions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    identity TEXT NOT NULL,
                    muted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, user_id, identity)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_subs_identity ON subscriptions (guild_id, identity)"
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
                """SELECT channel_id, message_id, author_id, full_url, full_url AS url,
                          jump_url, identity, slug, clean_title, first_posted_at
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
        limit = max(1, min(int(limit), 100))
        return await asyncio.to_thread(self._search_novels_sync, guild_id, query, limit)

    def _search_novels_sync(
        self, guild_id: int, query: str, limit: int
    ) -> list[dict[str, Any]]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT identity, slug, clean_title, full_url AS url,
                          channel_id, MIN(message_id) AS message_id, first_posted_at
                   FROM tracked_novels
                   WHERE guild_id = ? AND (
                       identity LIKE ? ESCAPE '\\' OR slug LIKE ? ESCAPE '\\'
                       OR clean_title LIKE ? ESCAPE '\\' OR full_url LIKE ? ESCAPE '\\'
                       OR identity IN (
                           SELECT identity FROM novel_meta WHERE title LIKE ? ESCAPE '\\')
                   ) AND {_NOISE_SQL}
                   GROUP BY identity
                   ORDER BY MIN(message_id) ASC LIMIT ?""",
                (guild_id, pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    # ------------------------------------------------------------ metadata cache

    @staticmethod
    def identity_hash(identity: str) -> str:
        """Short stable id used in persistent button custom_ids (max 100 chars)."""
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]

    async def get_meta(self, identity: str) -> dict[str, Any] | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_meta_sync, identity)

    def _get_meta_sync(self, identity: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM novel_meta WHERE identity = ?", (identity,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def get_meta_many(self, identities: list[str]) -> dict[str, dict[str, Any]]:
        """Cached metadata only (no network). Missing identities are omitted."""
        await self.initialize()
        if not identities:
            return {}
        return await asyncio.to_thread(self._get_meta_many_sync, list(identities))

    def _get_meta_many_sync(self, identities: list[str]) -> dict[str, dict[str, Any]]:
        connection = self._connect()
        try:
            found: dict[str, dict[str, Any]] = {}
            for start in range(0, len(identities), 500):  # stay under SQLite's variable cap
                chunk = identities[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for row in connection.execute(
                    f"SELECT * FROM novel_meta WHERE identity IN ({marks})", chunk
                ):
                    found[row["identity"]] = dict(row)
            return found
        finally:
            connection.close()

    async def identity_for_hash(self, identity_hash: str) -> str | None:
        await self.initialize()
        return await asyncio.to_thread(self._identity_for_hash_sync, identity_hash)

    def _identity_for_hash_sync(self, identity_hash: str) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT identity FROM novel_meta WHERE identity_hash = ? LIMIT 1",
                (identity_hash,),
            ).fetchone()
            if row:
                return row["identity"]
            # Fall back to the posted-link table (meta row may not exist yet).
            for row in connection.execute("SELECT DISTINCT identity FROM tracked_novels"):
                if row["identity"] and self.identity_hash(row["identity"]) == identity_hash:
                    return row["identity"]
            return None
        finally:
            connection.close()

    async def save_meta(
        self,
        *,
        identity: str,
        url: str,
        fields: dict[str, Any],
        failed: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Merge freshly parsed fields into the cache.

        Returns (stored_row, latest_chapter_changed). A change is only reported
        when a previous non-empty chapter existed, so the first fetch of a novel
        becomes a silent baseline instead of a false "new release".
        """
        await self.initialize()
        return await asyncio.to_thread(self._save_meta_sync, identity, url, fields, failed)

    _META_FIELDS = (
        "title", "author", "status", "description", "cover_url", "site_name",
        "icon_url", "latest_chapter", "latest_chapter_url", "released_at",
    )

    def _save_meta_sync(
        self, identity: str, url: str, fields: dict[str, Any], failed: bool
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        connection = self._connect()
        try:
            old = connection.execute(
                "SELECT * FROM novel_meta WHERE identity = ?", (identity,)
            ).fetchone()
            old_d = dict(old) if old else {}
            if failed:
                connection.execute(
                    """INSERT INTO novel_meta (identity, identity_hash, url, fetched_at, fail_count)
                       VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(identity) DO UPDATE SET
                           fetched_at = excluded.fetched_at,
                           fail_count = novel_meta.fail_count + 1""",
                    (identity, self.identity_hash(identity), url, now),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM novel_meta WHERE identity = ?", (identity,)
                ).fetchone()
                return dict(row), False

            merged = {
                key: (fields.get(key) or old_d.get(key)) for key in self._META_FIELDS
            }
            old_chapter = old_d.get("latest_chapter")
            new_chapter = merged["latest_chapter"]
            changed = bool(old_chapter and new_chapter and old_chapter != new_chapter)
            changed_at = now if changed else old_d.get("changed_at")
            connection.execute(
                """INSERT INTO novel_meta (
                       identity, identity_hash, url, title, author, status, description,
                       cover_url, site_name, icon_url, latest_chapter, latest_chapter_url,
                       released_at, fetched_at, changed_at, fail_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(identity) DO UPDATE SET
                       url = excluded.url, title = excluded.title, author = excluded.author,
                       status = excluded.status, description = excluded.description,
                       cover_url = excluded.cover_url, site_name = excluded.site_name,
                       icon_url = excluded.icon_url, latest_chapter = excluded.latest_chapter,
                       latest_chapter_url = excluded.latest_chapter_url,
                       released_at = excluded.released_at, fetched_at = excluded.fetched_at,
                       changed_at = excluded.changed_at, fail_count = 0""",
                (
                    identity, self.identity_hash(identity), url,
                    merged["title"], merged["author"], merged["status"], merged["description"],
                    merged["cover_url"], merged["site_name"], merged["icon_url"],
                    merged["latest_chapter"], merged["latest_chapter_url"],
                    merged["released_at"], now, changed_at,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM novel_meta WHERE identity = ?", (identity,)
            ).fetchone()
            return dict(row), changed
        finally:
            connection.close()

    # ------------------------------------------------------------ subscriptions

    async def subscribe(self, *, guild_id: int, user_id: int, identity: str) -> bool:
        """Returns True if newly subscribed, False if already tracking."""
        await self.initialize()
        return await asyncio.to_thread(self._subscribe_sync, guild_id, user_id, identity)

    def _subscribe_sync(self, guild_id: int, user_id: int, identity: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO subscriptions (guild_id, user_id, identity, muted, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (guild_id, user_id, identity, time.time()),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    async def unsubscribe(self, *, guild_id: int, user_id: int, identity: str) -> bool:
        await self.initialize()
        return await asyncio.to_thread(self._unsubscribe_sync, guild_id, user_id, identity)

    def _unsubscribe_sync(self, guild_id: int, user_id: int, identity: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND user_id = ? AND identity = ?",
                (guild_id, user_id, identity),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    async def toggle_mute(
        self, *, guild_id: int, user_id: int, identity: str
    ) -> bool | None:
        """Flip the mute flag. Returns the new muted state, or None if not tracking."""
        await self.initialize()
        return await asyncio.to_thread(self._toggle_mute_sync, guild_id, user_id, identity)

    def _toggle_mute_sync(self, guild_id: int, user_id: int, identity: str) -> bool | None:
        connection = self._connect()
        try:
            cursor = connection.execute(
                """UPDATE subscriptions SET muted = 1 - muted
                   WHERE guild_id = ? AND user_id = ? AND identity = ?""",
                (guild_id, user_id, identity),
            )
            connection.commit()
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT muted FROM subscriptions WHERE guild_id = ? AND user_id = ? AND identity = ?",
                (guild_id, user_id, identity),
            ).fetchone()
            return bool(row["muted"]) if row else None
        finally:
            connection.close()

    async def get_subscription(
        self, *, guild_id: int, user_id: int, identity: str
    ) -> dict[str, Any] | None:
        await self.initialize()
        return await asyncio.to_thread(self._get_subscription_sync, guild_id, user_id, identity)

    def _get_subscription_sync(
        self, guild_id: int, user_id: int, identity: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE guild_id = ? AND user_id = ? AND identity = ?",
                (guild_id, user_id, identity),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    async def count_subscribers(self, *, guild_id: int, identity: str) -> int:
        await self.initialize()
        return await asyncio.to_thread(self._count_subscribers_sync, guild_id, identity)

    def _count_subscribers_sync(self, guild_id: int, identity: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE guild_id = ? AND identity = ?",
                (guild_id, identity),
            ).fetchone()
            return int(row["n"])
        finally:
            connection.close()

    async def list_subscribers(self, *, guild_id: int, identity: str) -> list[dict[str, Any]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_subscribers_sync, guild_id, identity)

    def _list_subscribers_sync(self, guild_id: int, identity: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT user_id, muted FROM subscriptions WHERE guild_id = ? AND identity = ? ORDER BY created_at",
                (guild_id, identity),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    async def list_user_subscriptions(
        self, *, guild_id: int, user_id: int
    ) -> list[dict[str, Any]]:
        """A user's tracked novels, newest subscription first, joined to the index."""
        await self.initialize()
        return await asyncio.to_thread(self._list_user_subscriptions_sync, guild_id, user_id)

    def _list_user_subscriptions_sync(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT s.identity, s.muted, s.created_at,
                          t.full_url AS url, t.clean_title, t.slug,
                          t.channel_id, t.message_id
                   FROM subscriptions s
                   LEFT JOIN tracked_novels t
                          ON t.guild_id = s.guild_id AND t.identity = s.identity
                         AND t.message_id = (
                             SELECT MIN(message_id) FROM tracked_novels
                             WHERE guild_id = s.guild_id AND identity = s.identity)
                   WHERE s.guild_id = ? AND s.user_id = ?
                   GROUP BY s.identity
                   ORDER BY s.created_at DESC""",
                (guild_id, user_id),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    async def list_subscribed_identities(self) -> list[dict[str, Any]]:
        """Every (identity, guild) pair with at least one subscriber."""
        await self.initialize()
        return await asyncio.to_thread(self._list_subscribed_identities_sync)

    def _list_subscribed_identities_sync(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT s.identity, s.guild_id,
                          (SELECT full_url FROM tracked_novels t
                            WHERE t.guild_id = s.guild_id AND t.identity = s.identity
                            ORDER BY message_id ASC LIMIT 1) AS url,
                          (SELECT channel_id FROM tracked_novels t
                            WHERE t.guild_id = s.guild_id AND t.identity = s.identity
                            ORDER BY message_id ASC LIMIT 1) AS channel_id
                   FROM subscriptions s
                   GROUP BY s.guild_id, s.identity"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    async def list_indexed(self, *, guild_id: int, limit: int = 500) -> list[dict[str, Any]]:
        """Every distinct novel in the server index, most recently posted first."""
        await self.initialize()
        limit = max(1, min(int(limit), 2000))
        return await asyncio.to_thread(self._list_indexed_sync, guild_id, limit)

    def _list_indexed_sync(self, guild_id: int, limit: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT identity, full_url AS url, clean_title, slug, channel_id,
                          MIN(message_id) AS message_id, MAX(message_id) AS last_message_id,
                          COUNT(*) AS posts
                   FROM tracked_novels
                   WHERE guild_id = ? AND identity != '' AND {_NOISE_SQL}
                   GROUP BY identity
                   ORDER BY MAX(message_id) DESC LIMIT ?""",
                (guild_id, limit),
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
        limit = max(1, min(int(limit), 200))
        return await asyncio.to_thread(self._find_duplicates_sync, guild_id, limit)

    def _find_duplicates_sync(self, guild_id: int, limit: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""SELECT t.identity, t.full_url AS url, t.clean_title, t.slug,
                          t.channel_id, t.message_id, d.cnt AS count
                   FROM tracked_novels t
                   JOIN (
                       SELECT identity, MIN(message_id) AS first_id, COUNT(*) AS cnt
                       FROM tracked_novels
                       WHERE guild_id = ? AND identity != '' AND {_NOISE_SQL}
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
