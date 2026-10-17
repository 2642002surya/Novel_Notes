import os
import aiosqlite
import json
import time

DB_PATH = os.getenv("DATABASE_PATH", "novel_scanner.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        # Existing scanner tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scanned_messages (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER DEFAULT NULL,
                auto_scan_enabled INTEGER DEFAULT 1,
                last_scan_timestamp REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignored_novels (
                guild_id INTEGER NOT NULL,
                novel_slug TEXT NOT NULL,
                ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, novel_slug)
            )
        """)

        # Master Novels Table with Full Compatibility
        await db.execute("""
            CREATE TABLE IF NOT EXISTS novels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                category TEXT DEFAULT 'Unclassified',
                reading_status TEXT DEFAULT 'Unassigned',
                theme TEXT DEFAULT 'Normal / Other',
                destination_category TEXT DEFAULT '📌・The Archive Gates',
                is_completed INTEGER DEFAULT 0,
                is_favorite INTEGER DEFAULT 0,
                is_uncertain INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS novel_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id INTEGER,
                original_url TEXT UNIQUE NOT NULL,
                domain TEXT NOT NULL,
                url_type TEXT NOT NULL,
                chapter_num REAL DEFAULT -1.0,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT,
                category_name TEXT,
                is_source_completed INTEGER DEFAULT 0,
                message_id INTEGER,
                message_url TEXT,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (novel_id) REFERENCES novels (id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS posted_entries (
                source_id INTEGER PRIMARY KEY,
                target_guild_id INTEGER NOT NULL,
                target_channel_id INTEGER NOT NULL,
                posted_message_id INTEGER,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_id) REFERENCES novel_sources (id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_channel ON scanned_messages(channel_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_novel_slug ON novels(canonical_slug)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_source_url ON novel_sources(original_url)")
        await db.commit()

async def upsert_novel_and_source(data: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, reading_status, is_completed, is_favorite FROM novels WHERE canonical_slug = ?", (data["slug"],)) as cur:
            novel = await cur.fetchone()

        theme = data.get("theme", data.get("default_category", "Normal / Other"))
        reading_status = data.get("reading_status", "Unassigned")
        dest_cat = data.get("destination_category", "📌・The Archive Gates")
        is_completed = data.get("is_completed", 1 if data.get("is_source_completed") else 0)
        is_favorite = data.get("is_favorite", 0)

        if novel:
            novel_id = novel["id"]
            if is_favorite and not novel["is_favorite"]:
                await db.execute("UPDATE novels SET is_favorite = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (novel_id,))
            if is_completed and not novel["is_completed"]:
                await db.execute("""
                    UPDATE novels SET is_completed = 1, reading_status = 'Already Read', 
                    theme = ?, category = ?, destination_category = '🏆・The Hall of Ascended Legends', updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (theme, theme, novel_id))
        else:
            cur = await db.execute("""
                INSERT INTO novels (
                    canonical_slug, title, category, reading_status, theme, destination_category,
                    is_completed, is_favorite, is_uncertain, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (
                data["slug"], data["title"], theme, reading_status, theme, dest_cat,
                is_completed, is_favorite, 1 if data["url_type"] == "unknown" else 0
            ))
            novel_id = cur.lastrowid

        await db.execute("""
            INSERT OR IGNORE INTO novel_sources (
                novel_id, original_url, domain, url_type, chapter_num,
                guild_id, channel_id, channel_name, category_name,
                is_source_completed, message_id, message_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            novel_id, data["url"], data["domain"], data["url_type"], data["chapter_num"],
            data["guild_id"], data["channel_id"], data["channel_name"], data.get("category_name"),
            is_completed, data.get("message_id"), data.get("message_url")
        ))
        await db.commit()
        return novel_id

async def fetch_dashboard_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) as total_novels FROM novels") as cur:
            total_novels = (await cur.fetchone())["total_novels"]
        async with db.execute("SELECT COUNT(*) as total_links FROM novel_sources") as cur:
            total_links = (await cur.fetchone())["total_links"]
        async with db.execute("SELECT COUNT(*) as classified FROM novels WHERE category != 'Unclassified' OR reading_status != 'Unassigned'") as cur:
            classified = (await cur.fetchone())["classified"]
        async with db.execute("SELECT COUNT(*) as unclassified FROM novels WHERE category = 'Unclassified' AND reading_status = 'Unassigned'") as cur:
            unclassified = (await cur.fetchone())["unclassified"]
        async with db.execute("SELECT COUNT(*) as completed FROM novels WHERE is_completed = 1") as cur:
            completed = (await cur.fetchone())["completed"]
        async with db.execute("SELECT COUNT(*) as favorites FROM novels WHERE is_favorite = 1") as cur:
            favorites = (await cur.fetchone())["favorites"]

        return {
            "total_novels": total_novels,
            "total_links": total_links,
            "classified": classified,
            "unclassified": unclassified,
            "completed": completed,
            "favorites": favorites
        }

async def fetch_novels_paginated(search="", category="ALL", domain="ALL", url_type="ALL", status="ALL", page=1, per_page=20):
    offset = (page - 1) * per_page
    query = """
        SELECT n.*, COUNT(s.id) as link_count,
               GROUP_CONCAT(DISTINCT s.domain) as domains,
               GROUP_CONCAT(DISTINCT s.url_type) as url_types
        FROM novels n
        LEFT JOIN novel_sources s ON n.id = s.novel_id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (n.title LIKE ? OR n.canonical_slug LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if category != "ALL":
        query += " AND (n.category = ? OR n.theme = ?)"
        params.extend([category, category])
    if domain != "ALL":
        query += " AND s.domain = ?"
        params.append(domain)

    query += " GROUP BY n.id ORDER BY n.id DESC LIMIT ? OFFSET ?"
    params.extend([per_page, offset])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            novels = [dict(r) for r in await cur.fetchall()]

        for novel in novels:
            async with db.execute("SELECT * FROM novel_sources WHERE novel_id = ?", (novel["id"],)) as scur:
                novel["sources"] = [dict(s) for s in await scur.fetchall()]

        return novels

async def update_novel_details(novel_id: int, title: str, category: str, is_completed: bool = None):
    async with aiosqlite.connect(DB_PATH) as db:
        comp_val = 1 if is_completed else 0 if is_completed is not None else None
        if comp_val is not None:
            await db.execute("""
                UPDATE novels 
                SET title = ?, category = ?, theme = ?, is_completed = ?, status = 'reviewed', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (title.strip(), category.strip(), category.strip(), comp_val, novel_id))
        else:
            await db.execute("""
                UPDATE novels 
                SET title = ?, category = ?, theme = ?, status = 'reviewed', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (title.strip(), category.strip(), category.strip(), novel_id))
        await db.commit()

async def bulk_classify_novels(novel_ids: list[int], category: str):
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in novel_ids)
        await db.execute(f"""
            UPDATE novels 
            SET category = ?, theme = ?, status = 'reviewed', updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
        """, [category, category] + novel_ids)
        await db.commit()

# Scanner helper queries
async def is_novel_ignored(guild_id: int, novel_slug: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM ignored_novels WHERE guild_id = ? AND novel_slug = ?", (guild_id, novel_slug.lower())) as cur:
            return (await cur.fetchone()) is not None

async def ignore_novel(guild_id: int, novel_slug: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO ignored_novels (guild_id, novel_slug) VALUES (?, ?)", (guild_id, novel_slug.lower()))
        await db.commit()

async def unignore_novel(guild_id: int, novel_slug: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM ignored_novels WHERE guild_id = ? AND novel_slug = ?", (guild_id, novel_slug.lower()))
        await db.commit()
        return cur.rowcount > 0

async def get_ignored_slugs(guild_id: int) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT novel_slug FROM ignored_novels WHERE guild_id = ?", (guild_id,)) as cur:
            return {r[0] for r in await cur.fetchall()}

async def mark_messages_scanned(records: list[tuple[int, int, int]]):
    if not records:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("INSERT OR IGNORE INTO scanned_messages (message_id, channel_id, guild_id) VALUES (?, ?, ?)", records)
        await db.commit()

async def get_guild_settings(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT log_channel_id, auto_scan_enabled, last_scan_timestamp FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return {"log_channel_id": row[0], "auto_scan_enabled": bool(row[1]), "last_scan_timestamp": row[2]}
            await db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            await db.commit()
            return {"log_channel_id": None, "auto_scan_enabled": True, "last_scan_timestamp": 0.0}

async def update_scan_timestamp(guild_id: int, timestamp: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_settings (guild_id, last_scan_timestamp) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET last_scan_timestamp = excluded.last_scan_timestamp
        """, (guild_id, timestamp))
        await db.commit()