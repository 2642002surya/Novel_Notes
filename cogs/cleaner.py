from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

MAX_MESSAGES_EXAMINED = 5000


class PurgeConfirmationView(discord.ui.View):
    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who started this action can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Confirmed. Processing the action...",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Action cancelled.", view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content="Confirmation expired. No action was performed.",
                    view=self,
                )
            except discord.HTTPException:
                pass


class Cleaner(commands.Cog):
    """Message cleanup and channel management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_locks:
            self._channel_locks[channel_id] = asyncio.Lock()
        return self._channel_locks[channel_id]

    async def _check_permissions(
        self,
        ctx: commands.Context,
        *,
        require_manage_guild: bool = False,
        require_bot_manage_messages: bool = True,
    ) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.send("This command can only be used in a server.")
            return False
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command requires a standard text channel.")
            return False

        author_permissions = ctx.author.guild_permissions
        if require_manage_guild:
            allowed = author_permissions.administrator or author_permissions.manage_guild
            if not allowed:
                await ctx.send("You need Manage Server or Administrator permission.")
                return False
        else:
            allowed = author_permissions.administrator or author_permissions.manage_messages
            if not allowed:
                await ctx.send("You need Manage Messages or Administrator permission.")
                return False

        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("I could not verify my server permissions.")
            return False
        bot_permissions = ctx.channel.permissions_for(bot_member)
        if require_bot_manage_messages:
            if not bot_permissions.manage_messages:
                await ctx.send("I need Manage Messages permission in this channel.")
                return False
            if not bot_permissions.read_message_history:
                await ctx.send("I need Read Message History permission in this channel.")
                return False
        elif not bot_permissions.manage_channels:
            await ctx.send("I need Manage Channels permission in this channel.")
            return False
        return True

    async def _confirm_action(self, ctx: commands.Context, prompt: str) -> bool:
        view = PurgeConfirmationView(ctx.author.id)
        message = await ctx.send(prompt, view=view)
        view.message = message
        await view.wait()
        return view.confirmed

    async def _purge_messages(self, ctx: commands.Context, *, count: int, predicate=None) -> int:
        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            return 0

        deleted = 0
        command_message_id = ctx.message.id
        async with self._get_lock(channel.id):
            try:
                # Look back a bounded distance so a rare filter (e.g. `clean links`)
                # can never walk an entire channel history.
                async for message in channel.history(
                    limit=MAX_MESSAGES_EXAMINED, oldest_first=False
                ):
                    if deleted >= count:
                        break
                    if message.id == command_message_id:
                        continue
                    if predicate is not None and not predicate(message):
                        continue
                    try:
                        await message.delete()
                        deleted += 1
                    except discord.NotFound:
                        continue
                    except discord.Forbidden:
                        logger.warning(
                            "Missing permission to delete message %s in channel %s",
                            message.id,
                            channel.id,
                        )
                        break
                    except discord.HTTPException:
                        logger.exception("Failed to delete message %s", message.id)
                        await asyncio.sleep(0.5)
            except discord.Forbidden:
                await ctx.send("I cannot read this channel's message history.")
            except discord.HTTPException:
                logger.exception("Failed while cleaning channel %s", channel.id)
        return deleted

    async def _run_purge(self, ctx: commands.Context, *, count: int, predicate=None):
        if not await self._check_permissions(ctx):
            return
        if not 1 <= count <= 1000:
            await ctx.send("Choose a number between 1 and 1000.")
            return
        if count > 100:
            confirmed = await self._confirm_action(
                ctx,
                f"Delete up to **{count}** matching messages from this channel?",
            )
            if not confirmed:
                return
        deleted = await self._purge_messages(ctx, count=count, predicate=predicate)
        await ctx.send(f"Deleted **{deleted}** message(s).", delete_after=8)

    @commands.group(name="clean", aliases=["purge", "clear"], invoke_without_command=True)
    async def clean(self, ctx: commands.Context, count: int = 50):
        """Delete recent messages. Usage: i novel clean 50"""
        await self._run_purge(ctx, count=count)

    @clean.command(name="bots")
    async def clean_bots(self, ctx: commands.Context, count: int = 50):
        """Delete messages sent by bots."""
        await self._run_purge(ctx, count=count, predicate=lambda message: message.author.bot)

    @clean.command(name="users")
    async def clean_users(self, ctx: commands.Context, count: int = 50):
        """Delete messages sent by non-bot users."""
        await self._run_purge(ctx, count=count, predicate=lambda message: not message.author.bot)

    @clean.command(name="links")
    async def clean_links(self, ctx: commands.Context, count: int = 50):
        """Delete messages containing links."""
        await self._run_purge(
            ctx,
            count=count,
            predicate=lambda message: any(
                token in message.content.lower() for token in ("http://", "https://", "www.")
            ),
        )

    @clean.command(name="embeds")
    async def clean_embeds(self, ctx: commands.Context, count: int = 50):
        """Delete messages containing embeds."""
        await self._run_purge(ctx, count=count, predicate=lambda message: bool(message.embeds))

    @clean.command(name="attachments")
    async def clean_attachments(self, ctx: commands.Context, count: int = 50):
        """Delete messages containing attachments."""
        await self._run_purge(ctx, count=count, predicate=lambda message: bool(message.attachments))

    @commands.command(name="slowmode")
    async def slowmode(self, ctx: commands.Context, seconds: int):
        """Set channel slowmode from 0 to 21600 seconds."""
        if not await self._check_permissions(
            ctx, require_manage_guild=True, require_bot_manage_messages=False
        ):
            return
        if not 0 <= seconds <= 21600:
            await ctx.send("Slowmode must be between 0 and 21600 seconds.")
            return
        channel = ctx.channel
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("This command requires a standard text channel.")
            return
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
            await ctx.send(
                "Slowmode disabled." if seconds == 0 else f"Slowmode set to **{seconds}** second(s)."
            )
        except discord.Forbidden:
            await ctx.send("I do not have permission to edit this channel.")
        except discord.HTTPException:
            logger.exception("Failed to change slowmode")
            await ctx.send("Could not change slowmode.")

    @commands.command(name="nuke")
    async def nuke_channel(self, ctx: commands.Context):
        """Replace the current channel with a fresh copy; messages are not preserved."""
        if not await self._check_permissions(
            ctx, require_manage_guild=True, require_bot_manage_messages=False
        ):
            return
        channel = ctx.channel
        guild = ctx.guild
        if not isinstance(channel, discord.TextChannel) or guild is None:
            await ctx.send("This command requires a standard text channel.")
            return
        if not await self._confirm_action(
            ctx,
            "This will delete the current channel and create a replacement with the same basic settings. "
            "Messages will not be preserved. Continue?",
        ):
            return

        try:
            replacement = await guild.create_text_channel(
                name=channel.name,
                category=channel.category,
                topic=channel.topic,
                position=channel.position,
                nsfw=channel.nsfw,
                slowmode_delay=channel.slowmode_delay,
                overwrites=channel.overwrites,
                reason=f"Channel replacement requested by {ctx.author}",
            )
        except discord.Forbidden:
            await ctx.send("I could not create the replacement channel. Check Manage Channels permission.")
            return
        except discord.HTTPException:
            logger.exception("Failed to create replacement channel")
            await ctx.send("Could not create the replacement channel.")
            return

        try:
            await channel.delete(reason=f"Channel replaced by {ctx.author}")
        except discord.HTTPException:
            logger.exception("Failed to delete original channel")
            try:
                await replacement.delete(reason="Rollback after failed channel replacement")
            except discord.HTTPException:
                logger.exception("Failed to remove replacement channel")
            await ctx.send("I could not delete the original channel. The replacement may have been removed.")
            return

        try:
            await replacement.send(
                f"Channel recreated by {ctx.author.mention}. Previous messages were not preserved."
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Cleaner(bot))
