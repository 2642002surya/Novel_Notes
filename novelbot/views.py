"""Interactive Discord UI: paginated browser, pickers and release-alert buttons."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import discord

from .embeds import (
    Colors,
    error_embed,
    info_embed,
    jump_url,
    list_page_embed,
    novel_embed,
    novel_title,
    novel_updates_url,
    safe_url,
    success_embed,
    trunc,
    untracked_embed,
    host_of,
)
from .service import NovelService, get_service

logger = logging.getLogger("NovelGuard.Views")

VIEW_TIMEOUT = 180
_BACKGROUND: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive
SELECT_LIMIT = 25  # Discord's cap on options per select menu


# --------------------------------------------------------------------- helpers


def make_button(
    label: str | None,
    callback: Callable[[discord.Interaction], Awaitable[None]],
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: str | None = None,
    row: int | None = None,
    disabled: bool = False,
) -> discord.ui.Button:
    button: discord.ui.Button = discord.ui.Button(
        label=label or None, style=style, emoji=emoji, row=row, disabled=disabled
    )
    button.callback = callback  # type: ignore[assignment]
    return button


def build_link_buttons(
    *,
    title: str,
    url: str | None,
    meta: dict[str, Any] | None,
    jump: str | None = None,
    row: int | None = None,
) -> list[discord.ui.Button]:
    """Direct-link buttons: latest chapter, source page, Novel Updates, original post."""
    buttons: list[discord.ui.Button] = []
    chapter_url = safe_url((meta or {}).get("latest_chapter_url"))
    if chapter_url:
        buttons.append(
            discord.ui.Button(
                label="View Chapter", emoji="📖", style=discord.ButtonStyle.link,
                url=chapter_url, row=row,
            )
        )
    source = safe_url(url)
    if source:
        buttons.append(
            discord.ui.Button(
                label="Source Page", emoji="🌐", style=discord.ButtonStyle.link,
                url=source, row=row,
            )
        )
    nu = novel_updates_url(title)
    if nu:
        buttons.append(
            discord.ui.Button(
                label="Novel Updates", emoji="🔎", style=discord.ButtonStyle.link,
                url=nu, row=row,
            )
        )
    if jump:
        buttons.append(
            discord.ui.Button(
                label="Original Post", emoji="💬", style=discord.ButtonStyle.link,
                url=jump, row=row,
            )
        )
    return buttons


class OwnedView(discord.ui.View):
    """A view only its invoker may use; disables itself when it times out."""

    def __init__(self, author_id: int, *, timeout: float | None = VIEW_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "This menu belongs to someone else. Run the command yourself to get your own.",
                    title="Not your menu",
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[union-attr]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("View expired before it could be disabled.")

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        logger.error("Error in view %s", type(self).__name__, exc_info=error)
        embed = error_embed("That action failed. Please try again.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------- browser view


class BrowserView(OwnedView):
    """Paginated list of novels with a select menu that opens a detail card.

    List mode:   ⏮ ◀ [Page x of y] ▶ ⏭   +   select menu of this page's novels
    Detail mode: Track/Unsubscribe, Mute Pings, Back  +  link buttons
    """

    def __init__(
        self,
        service: NovelService,
        *,
        author_id: int,
        guild_id: int,
        entries: list[dict[str, Any]],
        title: str,
        color: discord.Color,
        per_page: int = 5,
        intro: str | None = None,
        empty_text: str = "Nothing to show.",
        open_identity: str | None = None,
        drop_on_untrack: bool = False,
    ) -> None:
        super().__init__(author_id)
        self.service = service
        self.guild_id = guild_id
        self.entries = entries
        self.title = title
        self.color = color
        self.per_page = max(1, min(per_page, SELECT_LIMIT))
        self.intro = intro
        self.empty_text = empty_text
        self.drop_on_untrack = drop_on_untrack
        self.page = 0
        self.mode = "list"
        self._detail: dict[str, Any] | None = None
        self._start_identity = open_identity

    # ---------------------------------------------------------------- paging

    @property
    def page_count(self) -> int:
        return max(1, -(-len(self.entries) // self.per_page))

    def _page_entries(self) -> list[dict[str, Any]]:
        return self.entries[self.page * self.per_page : (self.page + 1) * self.per_page]

    def list_embed(self) -> discord.Embed:
        return list_page_embed(
            title=self.title,
            color=self.color,
            entries=self.entries,
            page=self.page,
            per_page=self.per_page,
            intro=self.intro,
            empty_text=self.empty_text,
        )

    def _build_list_items(self) -> None:
        self.clear_items()
        last = self.page_count - 1
        multi = self.page_count > 1
        self.add_item(make_button("", self._go_first, emoji="⏮", row=0, disabled=not multi or self.page == 0))
        self.add_item(make_button("", self._go_prev, emoji="◀", row=0, disabled=not multi or self.page == 0))
        self.add_item(
            make_button(f"Page {self.page + 1} of {self.page_count}", self._noop, row=0, disabled=True)
        )
        self.add_item(make_button("", self._go_next, emoji="▶", row=0, disabled=not multi or self.page >= last))
        self.add_item(make_button("", self._go_last, emoji="⏭", row=0, disabled=not multi or self.page >= last))

        chunk = self._page_entries()
        if chunk:
            base = self.page * self.per_page
            options = []
            for i, entry in enumerate(chunk):
                meta = entry.get("meta") or {}
                detail_bits = [b for b in (host_of(entry.get("url")), meta.get("status")) if b]
                options.append(
                    discord.SelectOption(
                        label=trunc(f"{base + i + 1}. {entry['title']}", 100),
                        value=str(base + i),
                        description=trunc(" • ".join(detail_bits), 100) or None,
                        emoji="📖",
                    )
                )
            select = discord.ui.Select(
                placeholder="Open a novel for details and actions…",
                min_values=1,
                max_values=1,
                options=options,
                row=1,
            )
            select.callback = self._on_select  # type: ignore[assignment]
            self.add_item(select)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _show_page(self, interaction: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self.page_count - 1))
        self._build_list_items()
        await interaction.response.edit_message(embed=self.list_embed(), view=self)

    async def _go_first(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, 0)

    async def _go_prev(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page - 1)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page + 1)

    async def _go_last(self, interaction: discord.Interaction) -> None:
        await self._show_page(interaction, self.page_count - 1)

    # ---------------------------------------------------------------- detail

    def _find_entry(self, identity: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e.get("identity") == identity), None)

    async def _load_detail(self, entry: dict[str, Any], flash: str | None = None) -> discord.Embed:
        """Fetch (cached) metadata + tracking state, rebuild detail items, return the card."""
        identity = entry["identity"]
        meta = await self.service.get_meta(identity, entry.get("url"))
        entry["meta"] = meta or entry.get("meta")
        entry["title"] = novel_title(entry, entry["meta"])

        db = self.service.database
        user_id = self.author_id
        sub = await db.get_subscription(guild_id=self.guild_id, user_id=user_id, identity=identity)
        count = await db.count_subscribers(guild_id=self.guild_id, identity=identity)
        self._detail = {"identity": identity, "sub": sub, "flash": flash}
        self.mode = "detail"
        self._build_detail_items(entry, sub)

        if flash == "tracked":
            return novel_embed(
                row=entry, meta=entry["meta"], color=Colors.SUCCESS, sub=sub,
                subscriber_count=count, heading="✅ Now tracking:",
            )
        if flash == "untracked":
            card = untracked_embed(entry["title"], meta=entry["meta"], url=entry.get("url"))
            card.add_field(name="🔔 Tracking", value="▫️ Not tracking", inline=True)
            return card
        return novel_embed(
            row=entry, meta=entry["meta"], color=Colors.INFO, sub=sub, subscriber_count=count
        )

    def _build_detail_items(self, entry: dict[str, Any], sub: dict[str, Any] | None) -> None:
        self.clear_items()
        if sub is None:
            self.add_item(
                make_button("Track", self._on_toggle_track, style=discord.ButtonStyle.success, emoji="🔔", row=0)
            )
        else:
            self.add_item(
                make_button("Unsubscribe", self._on_toggle_track, style=discord.ButtonStyle.danger, emoji="🔕", row=0)
            )
            self.add_item(
                make_button(
                    "Unmute Pings" if sub.get("muted") else "Mute Pings",
                    self._on_toggle_mute,
                    emoji="🔔" if sub.get("muted") else "🔇",
                    row=0,
                )
            )
        if self.entries and not (self._start_identity and len(self.entries) == 1):
            self.add_item(make_button("Back", self._on_back, emoji="◀", row=0))
        for button in build_link_buttons(
            title=entry["title"],
            url=entry.get("url"),
            meta=entry.get("meta"),
            jump=jump_url(self.guild_id, entry),
            row=1,
        ):
            self.add_item(button)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        select = next(i for i in self.children if isinstance(i, discord.ui.Select))
        entry = self.entries[int(select.values[0])]
        await interaction.response.defer()
        embed = await self._load_detail(entry)
        await interaction.edit_original_response(embed=embed, view=self)

    async def _current_entry(self) -> dict[str, Any] | None:
        if not self._detail:
            return None
        return self._find_entry(self._detail["identity"])

    async def _on_toggle_track(self, interaction: discord.Interaction) -> None:
        entry = await self._current_entry()
        if entry is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        db = self.service.database
        identity = entry["identity"]
        if self._detail and self._detail["sub"] is None:
            await db.subscribe(guild_id=self.guild_id, user_id=self.author_id, identity=identity)
            entry.pop("_dropped", None)
            flash = "tracked"
        else:
            await db.unsubscribe(guild_id=self.guild_id, user_id=self.author_id, identity=identity)
            flash = "untracked"
            if self.drop_on_untrack:
                entry["_dropped"] = True
        embed = await self._load_detail(entry, flash=flash)
        await interaction.edit_original_response(embed=embed, view=self)

    async def _on_toggle_mute(self, interaction: discord.Interaction) -> None:
        entry = await self._current_entry()
        await interaction.response.defer()
        if entry is None:
            return
        muted = await self.service.database.toggle_mute(
            guild_id=self.guild_id, user_id=self.author_id, identity=entry["identity"]
        )
        flash = None
        embed = await self._load_detail(entry, flash=flash)
        if muted is not None:
            embed.set_footer(
                text=("🔕 Pings muted — you stay subscribed but won't be mentioned."
                      if muted else "🔔 Pings back on — you'll be mentioned for new chapters.")
            )
        await interaction.edit_original_response(embed=embed, view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if self.drop_on_untrack:
            self.entries = [e for e in self.entries if not e.get("_dropped")]
        self.mode = "list"
        self._detail = None
        self.page = max(0, min(self.page, self.page_count - 1))
        self._build_list_items()
        await interaction.response.edit_message(embed=self.list_embed(), view=self)

    # ---------------------------------------------------------------- sending

    async def send(
        self, destination: Any, *, flash: str | None = None, **kwargs: Any
    ) -> discord.Message:
        """Send via a ``ctx.send``-style callable and remember the message.

        With ``open_identity`` the view starts on that novel's card (``flash`` may
        be "tracked" to show the green confirmation); otherwise on page 1.
        """
        if self._start_identity:
            entry = self._find_entry(self._start_identity)
            if entry is not None:
                embed = await self._load_detail(entry, flash=flash)
                self.message = await destination(embed=embed, view=self, **kwargs)
                return self.message
        self._build_list_items()
        self.message = await destination(embed=self.list_embed(), view=self, **kwargs)
        return self.message


# ------------------------------------------------------------- match picker


class MatchPickerView(OwnedView):
    """Multi-select menu to choose which of several matching novels to (un)track."""

    def __init__(
        self,
        service: NovelService,
        *,
        author_id: int,
        guild_id: int,
        entries: list[dict[str, Any]],
        action: str,  # "track" | "untrack"
    ) -> None:
        super().__init__(author_id, timeout=120)
        self.service = service
        self.guild_id = guild_id
        self.entries = entries[:SELECT_LIMIT]
        self.action = action

        options = []
        for i, entry in enumerate(self.entries):
            meta = entry.get("meta") or {}
            bits = [b for b in (host_of(entry.get("url")), meta.get("status")) if b]
            options.append(
                discord.SelectOption(
                    label=trunc(f"{i + 1}. {entry['title']}", 100),
                    value=str(i),
                    description=trunc(" • ".join(bits), 100) or None,
                    emoji="📖",
                )
            )
        verb = "track" if action == "track" else "stop tracking"
        select = discord.ui.Select(
            placeholder=f"Pick one or more novels to {verb}…",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )
        select.callback = self._on_pick  # type: ignore[assignment]
        self.add_item(select)
        self.add_item(make_button("Cancel", self._on_cancel, emoji="✖️", row=1))

    def prompt_embed(self, query: str) -> discord.Embed:
        verb = "track" if self.action == "track" else "untrack"
        lines = []
        for i, entry in enumerate(self.entries, 1):
            url = safe_url(entry.get("url"))
            title = trunc(entry["title"], 80)
            lines.append(f"**{i}.** [{title}]({url})" if url else f"**{i}.** {title}")
        embed = discord.Embed(
            title=f"🔎 {len(self.entries)} matches for “{trunc(query, 60)}”",
            description="\n".join(lines) + f"\n\nChoose which to {verb} from the menu below.",
            color=Colors.INFO,
        )
        cover = next(
            (safe_url((e.get("meta") or {}).get("cover_url")) for e in self.entries
             if safe_url((e.get("meta") or {}).get("cover_url"))),
            None,
        )
        if cover:
            embed.set_thumbnail(url=cover)
        return embed

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = next(i for i in self.children if isinstance(i, discord.ui.Select))
        chosen = [self.entries[int(v)] for v in select.values]
        await interaction.response.defer()
        db = self.service.database
        done: list[dict[str, Any]] = []
        for entry in chosen:
            if self.action == "track":
                await db.subscribe(guild_id=self.guild_id, user_id=self.author_id, identity=entry["identity"])
                done.append(entry)
                # Record a baseline chapter now so the first release check can spot changes.
                task = asyncio.create_task(self.service.get_meta(entry["identity"], entry.get("url")))
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)
            else:
                if await db.unsubscribe(guild_id=self.guild_id, user_id=self.author_id, identity=entry["identity"]):
                    done.append(entry)
        self.stop()
        lines = [f"• [{trunc(e['title'], 80)}]({safe_url(e.get('url')) or 'https://discord.com'})" for e in done]
        if self.action == "track":
            embed = success_embed("\n".join(lines) or "Nothing changed.",
                                  title=f"Now tracking {len(done)} novel{'s' if len(done) != 1 else ''}")
            embed.set_footer(text="Use the library command to see everything you track.")
        else:
            embed = discord.Embed(
                title=f"🔕 Stopped tracking {len(done)} novel{'s' if len(done) != 1 else ''}",
                description="\n".join(lines) or "Nothing changed.",
                color=Colors.ERROR,
            )
        cover = next((safe_url((e.get("meta") or {}).get("cover_url")) for e in done
                      if safe_url((e.get("meta") or {}).get("cover_url"))), None)
        if cover:
            embed.set_thumbnail(url=cover)
        await interaction.edit_original_response(embed=embed, view=None)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(embed=info_embed("Cancelled — nothing was changed."), view=None)


# ------------------------------------------------- persistent release buttons


class UnsubscribeButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"nb:unsub:(?P<h>[0-9a-f]{16})"
):
    """Release-alert button; keeps working after bot restarts."""

    def __init__(self, identity_hash: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Unsubscribe", emoji="🔕", style=discord.ButtonStyle.danger,
                custom_id=f"nb:unsub:{identity_hash}", row=0,
            )
        )
        self.identity_hash = identity_hash

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "UnsubscribeButton":
        return cls(match["h"])

    async def callback(self, interaction: discord.Interaction) -> None:
        service = get_service(interaction.client)  # type: ignore[arg-type]
        if interaction.guild_id is None:
            await interaction.response.send_message(embed=error_embed("Use this in a server."), ephemeral=True)
            return
        identity = await service.database.identity_for_hash(self.identity_hash)
        if identity is None:
            await interaction.response.send_message(embed=error_embed("I no longer know that novel."), ephemeral=True)
            return
        removed = await service.database.unsubscribe(
            guild_id=interaction.guild_id, user_id=interaction.user.id, identity=identity
        )
        meta = await service.database.get_meta(identity)
        title = novel_title(None, meta) if meta else "this novel"
        if removed:
            embed = untracked_embed(title, meta=meta, url=(meta or {}).get("url"))
        else:
            embed = info_embed("You weren't tracking this novel.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MuteButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"nb:mute:(?P<h>[0-9a-f]{16})"
):
    """Toggles whether *you* get pinged for this novel. The label is shared, the state is yours."""

    def __init__(self, identity_hash: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="Mute Pings", emoji="🔇", style=discord.ButtonStyle.secondary,
                custom_id=f"nb:mute:{identity_hash}", row=0,
            )
        )
        self.identity_hash = identity_hash

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match, /
    ) -> "MuteButton":
        return cls(match["h"])

    async def callback(self, interaction: discord.Interaction) -> None:
        service = get_service(interaction.client)  # type: ignore[arg-type]
        if interaction.guild_id is None:
            await interaction.response.send_message(embed=error_embed("Use this in a server."), ephemeral=True)
            return
        identity = await service.database.identity_for_hash(self.identity_hash)
        muted = (
            await service.database.toggle_mute(
                guild_id=interaction.guild_id, user_id=interaction.user.id, identity=identity
            )
            if identity
            else None
        )
        if muted is None:
            embed = info_embed("You aren't tracking this novel, so there is nothing to mute.")
        elif muted:
            embed = info_embed("You stay subscribed, but won't be mentioned for new chapters.",
                               title="🔕 Pings muted")
        else:
            embed = info_embed("You'll be mentioned again when a new chapter drops.",
                               title="🔔 Pings back on")
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_release_view(
    identity_hash: str, *, title: str, url: str | None, meta: dict[str, Any] | None
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(UnsubscribeButton(identity_hash))
    view.add_item(MuteButton(identity_hash))
    for button in build_link_buttons(title=title, url=url, meta=meta, row=1):
        if button.label != "Original Post":
            view.add_item(button)
    return view
