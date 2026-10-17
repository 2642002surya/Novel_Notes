import asyncio
import aiosqlite
import discord
from core.database import DB_PATH
from core.utils import normalize_channel_name

COMPLETED_CHANNELS = {
    "R-18 / Smut": "🔞・crimson-legends",
    "Harem": "👑・harem-legends",
    "Rich Protagonist": "💰・golden-legends",
    "Fast Cultivation": "⚡・rapid-ascension",
    "Romance": "💖・eternal-bonds",
    "Normal / Other": "📜・timeless-classics"
}

class ServerBuilder:
    def __init__(self, bot: discord.Client, target_guild: discord.Guild):
        self.bot = bot
        self.guild = target_guild

    async def preflight_audit(self) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM novels") as cur:
                novels = [dict(r) for r in await cur.fetchall()]
            async with db.execute("SELECT * FROM novel_sources") as cur:
                sources = [dict(r) for r in await cur.fetchall()]

        # Dedicated channels are required for Daily Chapter, Crimson Chapter, and Long Novel favorites
        dedicated_channel_count = 0
        for n in novels:
            if n["is_favorite"] or n["destination_category"] in ("⚡・Daily Chapter Realm", "🔞・Crimson Chapter Realm"):
                dedicated_channel_count += 1

        # Fixed management (5) + Completed collection (6) + Queue collection (1) + Moodbound collections (3)
        fixed_channels = 5 + 6 + 1 + 3
        planned_channels = fixed_channels + dedicated_channel_count
        existing_channels = len(self.guild.channels)
        projected = existing_channels + planned_channels

        return {
            "guild_name": self.guild.name,
            "existing_categories": len(self.guild.categories),
            "existing_channels": existing_channels,
            "planned_channels_to_create": planned_channels,
            "projected_total_channels": projected,
            "capacity_exceeded": projected > 500,
            "total_novels_ready": len(novels)
        }

    async def execute_build(self, progress_callback=None) -> dict:
        report = {"categories_created": 0, "channels_created": 0, "links_posted": 0}

        async def notify(status: str, percent: float):
            if progress_callback:
                await progress_callback(status, percent)

        await notify("Provisioning The Archive Gates...", 0.05)
        # 1. 📌・The Archive Gates
        gates_cat = await self._get_or_create_category("📌・The Archive Gates", report)
        await self._get_or_create_channel(gates_cat, "📜・archive-guide", report)
        await self._get_or_create_channel(gates_cat, "🔎・grand-novel-index", report)
        await self._get_or_create_channel(gates_cat, "⚙・archive-control", report)
        await self._get_or_create_channel(gates_cat, "📡・cultivation-announcements", report)
        await self._get_or_create_channel(gates_cat, "📥・unclassified-scrolls", report)

        # 2. 🏆・The Hall of Ascended Legends (Completed Novels)
        await notify("Provisioning The Hall of Ascended Legends...", 0.15)
        legends_cat = await self._get_or_create_category("🏆・The Hall of Ascended Legends", report)
        legends_channels = {}
        for theme_key, ch_name in COMPLETED_CHANNELS.items():
            legends_channels[theme_key] = await self._get_or_create_channel(legends_cat, ch_name, report)

        # Post Completed novels in batched format
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT n.title, s.id as source_id, s.original_url, n.theme
                FROM novels n
                JOIN novel_sources s ON n.id = s.novel_id
                WHERE n.is_completed = 1
                AND s.id NOT IN (SELECT source_id FROM posted_entries WHERE target_guild_id = ?)
            """, (self.guild.id,)) as cur:
                completed_items = await cur.fetchall()

        batches = {}
        for it in completed_items:
            batches.setdefault(it["theme"], []).append(it)

        for theme, items in batches.items():
            target_ch = legends_channels.get(theme, legends_channels["Normal / Other"])
            buf = f"**=== {theme} Ascended Legends ===**\n"
            pids = []
            for it in items:
                line = f"• **{it['title']}**: <{it['original_url']}>\n"
                if len(buf) + len(line) > 1900:
                    msg = await target_ch.send(buf)
                    await self._record_posts(pids, target_ch.id, msg.id)
                    report["links_posted"] += len(pids)
                    buf = f"**=== {theme} Ascended Legends (Cont.) ===**\n"
                    pids = []
                    await asyncio.sleep(1.0)
                buf += line
                pids.append(it["source_id"])

            if pids:
                msg = await target_ch.send(buf)
                await self._record_posts(pids, target_ch.id, msg.id)
                report["links_posted"] += len(pids)
                await asyncio.sleep(1.0)

        # 3. ⏳・The Heavenly Queue (In queue & To read combined)
        await notify("Provisioning The Heavenly Queue...", 0.30)
        queue_cat = await self._get_or_create_category("⏳・The Heavenly Queue", report)
        queue_ch = await self._get_or_create_channel(queue_cat, "📥・awaiting-awakening", report)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT n.title, s.id as source_id, s.original_url
                FROM novels n
                JOIN novel_sources s ON n.id = s.novel_id
                WHERE n.reading_status = 'Plan to Read' AND n.is_completed = 0
                AND s.id NOT IN (SELECT source_id FROM posted_entries WHERE target_guild_id = ?)
            """, (self.guild.id,)) as cur:
                queue_items = await cur.fetchall()

        q_buf = "**=== Heavenly Queue Awaiting Awakening ===**\n"
        q_pids = []
        for it in queue_items:
            line = f"• **{it['title']}**: <{it['original_url']}>\n"
            if len(q_buf) + len(line) > 1900:
                msg = await queue_ch.send(q_buf)
                await self._record_posts(q_pids, queue_ch.id, msg.id)
                report["links_posted"] += len(q_pids)
                q_buf = "**=== Heavenly Queue (Cont.) ===**\n"
                q_pids = []
                await asyncio.sleep(1.0)
            q_buf += line
            q_pids.append(it["source_id"])

        if q_pids:
            msg = await queue_ch.send(q_buf)
            await self._record_posts(q_pids, queue_ch.id, msg.id)
            report["links_posted"] += len(q_pids)
            await asyncio.sleep(1.0)

        # 4. Active Reading Realms & Dedicated Channels
        await notify("Provisioning Active Reading & Dedicated Series Channels...", 0.50)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM novels WHERE is_completed = 0 AND reading_status != 'Plan to Read'") as cur:
                active_novels = [dict(r) for r in await cur.fetchall()]

        for idx, novel in enumerate(active_novels, start=1):
            dest_cat_name = novel["destination_category"]
            cat_obj = await self._get_or_create_category_with_capacity(dest_cat_name, report)

            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM novel_sources WHERE novel_id = ?", (novel["id"],)) as scur:
                    novel_sources = [dict(r) for r in await scur.fetchall()]

            # Dedicated channel condition: Daily Chapter, Crimson Chapter, Favorite, or Chapter URL
            requires_dedicated = (
                novel["is_favorite"] 
                or dest_cat_name in ("⚡・Daily Chapter Realm", "🔞・Crimson Chapter Realm", "🌌・The Immortal Treasury")
                or any(s["url_type"] == "chapter" for s in novel_sources)
            )

            if requires_dedicated:
                ch_name = normalize_channel_name(novel["title"])
                novel_ch = await self._get_or_create_channel(cat_obj, ch_name, report)
                for s in novel_sources:
                    if not await self._is_posted(s["id"]):
                        ch_str = f"Chapter {s['chapter_num']:g}: " if s["chapter_num"] != -1.0 else ""
                        msg = await novel_ch.send(f"📖 **{novel['title']}** - {ch_str}<{s['original_url']}>")
                        await self._record_posts([s["id"]], novel_ch.id, msg.id)
                        report["links_posted"] += 1
                        await asyncio.sleep(0.8)
            else:
                # Unlinked book collection channel inside category
                collection_ch = await self._get_or_create_channel(cat_obj, "📥・realm-scrolls", report)
                for s in novel_sources:
                    if not await self._is_posted(s["id"]):
                        msg = await collection_ch.send(f"📚 **{novel['title']}**: <{s['original_url']}>")
                        await self._record_posts([s["id"]], collection_ch.id, msg.id)
                        report["links_posted"] += 1
                        await asyncio.sleep(0.8)

            ratio = 0.50 + (0.45 * (idx / max(len(active_novels), 1)))
            await notify(f"Provisioning ({idx}/{len(active_novels)}): {novel['title'][:25]}", ratio)

        await notify("Celestial Architecture built successfully!", 1.0)
        return report

    async def _get_or_create_category(self, name: str, report: dict) -> discord.CategoryChannel:
        for cat in self.guild.categories:
            if cat.name.lower() == name.lower():
                return cat
        report["categories_created"] += 1
        return await self.guild.create_category(name)

    async def _get_or_create_category_with_capacity(self, base_name: str, report: dict) -> discord.CategoryChannel:
        for num in range(1, 10):
            target = base_name if num == 1 else f"{base_name} {num}"
            cat = discord.utils.find(lambda c: c.name.lower() == target.lower(), self.guild.categories)
            if not cat:
                report["categories_created"] += 1
                return await self.guild.create_category(target)
            if len(cat.channels) < 48:
                return cat
        return cat

    async def _get_or_create_channel(self, category: discord.CategoryChannel, name: str, report: dict) -> discord.TextChannel:
        for ch in category.text_channels:
            if ch.name.lower() == name.lower():
                return ch
        report["channels_created"] += 1
        return await self.guild.create_text_channel(name, category=category)

    async def _is_posted(self, source_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM posted_entries WHERE source_id = ? AND target_guild_id = ?", (source_id, self.guild.id)) as cur:
                return (await cur.fetchone()) is not None

    async def _record_posts(self, source_ids: list[int], channel_id: int, message_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            entries = [(sid, self.guild.id, channel_id, message_id) for sid in source_ids]
            await db.executemany("INSERT OR IGNORE INTO posted_entries (source_id, target_guild_id, target_channel_id, posted_message_id) VALUES (?, ?, ?, ?)", entries)
            await db.commit()