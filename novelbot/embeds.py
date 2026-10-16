"""Embed builders and the colour scheme used across the bot."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote_plus, urlsplit

import discord

# --------------------------------------------------------------------- palette


class Colors:
    SUCCESS = discord.Color(0x2ECC71)  # green: new releases, successful tracking
    INFO = discord.Color(0x3498DB)  # blue: info, search results, help
    LIBRARY = discord.Color(0xF1C40F)  # gold: library / list views
    ERROR = discord.Color(0xE74C3C)  # red: errors, untracking
    WARNING = discord.Color(0xE67E22)  # orange: duplicate warnings


STATUS_ICONS = {
    "ongoing": "🟢",
    "completed": "✅",
    "hiatus": "⏸️",
    "on hold": "⏸️",
    "dropped": "❌",
}

BOT_NAME = "Novel Checking Bot"
NOVEL_UPDATES_SEARCH = "https://www.novelupdates.com/?s={query}&post_type=seriesplans"

# --------------------------------------------------------------------- helpers

_CHAPTER_SEGMENT = re.compile(r"^(?:chapter|chap|ch|episode|ep)[-_]?\d", re.I)


def trunc(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def clip(text: object, limit: int) -> str:
    """Like trunc() but keeps line breaks; for multi-line message bodies."""
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def safe_url(value: object) -> str | None:
    """Only http(s) URLs Discord will accept; anything else becomes None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2000 or " " in value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value


def host_of(url: str | None) -> str:
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.")


def base_url_of(url: str | None) -> str | None:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def derive_title(url: str | None, fallback: str = "Novel") -> str:
    """A readable title from a URL slug, skipping trailing chapter segments."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return fallback
    segments = [s for s in parts.path.split("/") if s]
    while len(segments) > 1 and _CHAPTER_SEGMENT.match(segments[-1]):
        segments.pop()
    slug = segments[-1] if segments else (parts.hostname or "")
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    words = re.sub(r"[-_+]+", " ", slug).split()
    title = " ".join(w.capitalize() for w in words)
    return title or fallback


def novel_title(row: Mapping[str, Any] | None, meta: Mapping[str, Any] | None) -> str:
    if meta and meta.get("title"):
        return trunc(meta["title"], 200)
    url = (row or {}).get("url") or (row or {}).get("full_url") or (meta or {}).get("url")
    return trunc(derive_title(url), 200)


def released_at(meta: Mapping[str, Any] | None) -> datetime | None:
    """Release time: what the page says, else when we noticed a new chapter."""
    if not meta:
        return None
    raw = meta.get("released_at")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if meta.get("changed_at"):
        return datetime.fromtimestamp(float(meta["changed_at"]), tz=timezone.utc)
    return None


def status_text(meta: Mapping[str, Any] | None) -> str:
    status = (meta or {}).get("status")
    if not status:
        return "❔ Unknown"
    return f"{STATUS_ICONS.get(str(status).lower(), '❔')} {status}"


def tracking_text(sub: Mapping[str, Any] | None, count: int) -> str:
    if sub is None:
        state = "▫️ Not tracking"
    elif sub.get("muted"):
        state = "🔕 Tracking (muted)"
    else:
        state = "🔔 Tracking"
    if count:
        state += f"\n{count} tracker{'s' if count != 1 else ''}"
    return state


def novel_updates_url(title: str) -> str | None:
    if not title or title == "Novel":
        return None
    return NOVEL_UPDATES_SEARCH.format(query=quote_plus(title[:100]))


def jump_url(guild_id: int, row: Mapping[str, Any] | None) -> str | None:
    if not row or not row.get("channel_id") or not row.get("message_id"):
        return None
    return f"https://discord.com/channels/{guild_id}/{int(row['channel_id'])}/{int(row['message_id'])}"


# ------------------------------------------------------------------ main embed


def novel_embed(
    *,
    row: Mapping[str, Any] | None,
    meta: Mapping[str, Any] | None,
    color: discord.Color = Colors.INFO,
    sub: Mapping[str, Any] | None = None,
    subscriber_count: int = 0,
    heading: str | None = None,
    description: str | None = None,
    tracking_label: str | None = None,
) -> discord.Embed:
    """The rich novel card: cover, author, status, latest chapter, release time, tracking."""
    meta = meta or {}
    url = safe_url((row or {}).get("url") or (row or {}).get("full_url") or meta.get("url"))
    title = novel_title(row, meta)

    embed = discord.Embed(
        title=trunc(f"{heading} {title}" if heading else title, 256),
        url=url,
        description=trunc(description or meta.get("description") or "", 600) or None,
        color=color,
    )

    source = meta.get("site_name") or host_of(url) or "Source"
    icon = safe_url(meta.get("icon_url"))
    author_kwargs: dict[str, Any] = {"name": trunc(source, 100), "url": base_url_of(url)}
    if icon:
        author_kwargs["icon_url"] = icon
    embed.set_author(**author_kwargs)

    cover = safe_url(meta.get("cover_url"))
    if cover:
        embed.set_thumbnail(url=cover)

    chapter = meta.get("latest_chapter")
    chapter_url = safe_url(meta.get("latest_chapter_url"))
    if chapter and chapter_url:
        chapter_value = f"[{trunc(chapter, 80)}]({chapter_url})"
    else:
        chapter_value = trunc(chapter, 100) if chapter else "Unknown"

    when = released_at(meta)
    if when:
        released_value = f"{discord.utils.format_dt(when, 'f')}\n{discord.utils.format_dt(when, 'R')}"
    else:
        released_value = "Unknown"

    embed.add_field(name="✍️ Author", value=trunc(meta.get("author") or "Unknown", 100), inline=True)
    embed.add_field(name="📌 Status", value=status_text(meta), inline=True)
    embed.add_field(name="📖 Latest Chapter", value=chapter_value, inline=True)
    embed.add_field(name="🕒 Released", value=released_value, inline=True)
    embed.add_field(
        name="🔔 Tracking",
        value=tracking_label or tracking_text(sub, subscriber_count),
        inline=True,
    )
    embed.add_field(name="🌐 Source", value=trunc(host_of(url) or "Unknown", 100), inline=True)

    embed.set_footer(
        text=f"{BOT_NAME} • {host_of(url) or 'novel'}",
        icon_url=icon,
    )
    return embed


def release_embed(
    *, row: Mapping[str, Any] | None, meta: Mapping[str, Any] | None, subscriber_count: int
) -> discord.Embed:
    chapter = (meta or {}).get("latest_chapter") or "a new chapter"
    embed = novel_embed(
        row=row,
        meta=meta,
        color=Colors.SUCCESS,
        heading="🆕",
        description=f"**{trunc(chapter, 120)}** has just been released.",
        tracking_label=f"{subscriber_count} tracker{'s' if subscriber_count != 1 else ''}",
    )
    icon = safe_url((meta or {}).get("icon_url"))
    if icon:
        embed.set_footer(text=f"{BOT_NAME} • New release", icon_url=icon)
    else:
        embed.set_footer(text=f"{BOT_NAME} • New release")
    return embed


# --------------------------------------------------------------- simple embeds


def info_embed(message: str, title: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=clip(message, 4000), color=Colors.INFO)


def success_embed(message: str, title: str | None = "Done") -> discord.Embed:
    return discord.Embed(
        title=f"✅ {title}" if title else None,
        description=clip(message, 4000),
        color=Colors.SUCCESS,
    )


def error_embed(message: str, title: str | None = "Something went wrong") -> discord.Embed:
    return discord.Embed(
        title=f"⚠️ {title}" if title else None,
        description=clip(message, 4000),
        color=Colors.ERROR,
    )


def untracked_embed(
    title: str, *, meta: Mapping[str, Any] | None = None, url: str | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title="🔕 Stopped tracking",
        description=f"You will no longer get updates for **{trunc(title, 200)}**.",
        color=Colors.ERROR,
        url=safe_url(url),
    )
    cover = safe_url((meta or {}).get("cover_url"))
    if cover:
        embed.set_thumbnail(url=cover)
    return embed


# ------------------------------------------------------------------ list pages


def entry_line(number: int, entry: Mapping[str, Any]) -> str:
    """One compact line for list pages: title link, status, chapter, tracking."""
    meta = entry.get("meta") or {}
    title = trunc(entry.get("title") or "Novel", 70)
    url = safe_url(entry.get("url"))
    head = f"**{number}.** [{title}]({url})" if url else f"**{number}.** {title}"

    bits: list[str] = []
    if meta.get("status"):
        bits.append(status_text(meta))
    if meta.get("latest_chapter"):
        bits.append(f"📖 {trunc(meta['latest_chapter'], 40)}")
    if entry.get("muted") is not None:
        bits.append("🔕 muted" if entry["muted"] else "🔔")
    if entry.get("badge"):
        bits.append(str(entry["badge"]))
    if not bits:
        bits.append(host_of(url) or "—")
    return head + "\n" + "┗ " + " · ".join(bits)


def list_page_embed(
    *,
    title: str,
    color: discord.Color,
    entries: Sequence[Mapping[str, Any]],
    page: int,
    per_page: int,
    intro: str | None = None,
    empty_text: str = "Nothing to show.",
) -> discord.Embed:
    total_pages = max(1, -(-len(entries) // per_page))
    page = max(0, min(page, total_pages - 1))
    chunk = entries[page * per_page : (page + 1) * per_page]

    lines = [entry_line(page * per_page + i + 1, e) for i, e in enumerate(chunk)]
    body = "\n\n".join(lines) if lines else empty_text
    description = f"{intro}\n\n{body}" if intro and lines else body

    embed = discord.Embed(title=trunc(title, 256), description=clip(description, 4000), color=color)
    for entry in chunk:
        cover = safe_url((entry.get("meta") or {}).get("cover_url"))
        if cover:
            embed.set_thumbnail(url=cover)
            break
    embed.set_footer(
        text=f"Page {page + 1} of {total_pages} • {len(entries)} result{'s' if len(entries) != 1 else ''}"
    )
    return embed
