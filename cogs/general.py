from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from novelbot.embeds import Colors

logger = logging.getLogger(__name__)


class General(commands.Cog):
    """General bot and server information commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="botinfo", aliases=["bot", "about"])
    async def about_bot(self, ctx: commands.Context):
        """Display information about the bot."""
        embed = discord.Embed(
            title="Novel Checking Bot",
            description=(
                "A Discord bot for tracking, indexing, and searching "
                "novel links shared across your server."
            ),
            color=Colors.INFO,
            timestamp=discord.utils.utcnow(),
        )

        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
            embed.add_field(
                name="Bot Account",
                value=f"{self.bot.user.mention}\n`{self.bot.user.id}`",
                inline=True,
            )

        embed.add_field(
            name="Library",
            value=f"`discord.py {discord.__version__}`",
            inline=True,
        )
        embed.add_field(
            name="Servers",
            value=f"`{len(self.bot.guilds):,}`",
            inline=True,
        )
        embed.add_field(
            name="Commands",
            value=f"`{len(self.bot.commands):,}`",
            inline=True,
        )

        latency = getattr(self.bot, "latency", None)
        if latency is not None:
            embed.add_field(
                name="Gateway Latency",
                value=f"`{latency * 1000:.0f} ms`",
                inline=True,
            )

        started_at = getattr(self.bot, "started_at", None)
        if started_at:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)

            uptime_seconds = max(
                0,
                int((discord.utils.utcnow() - started_at).total_seconds()),
            )
            days, remainder = divmod(uptime_seconds, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)

            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if minutes:
                parts.append(f"{minutes}m")
            parts.append(f"{seconds}s")

            embed.add_field(
                name="Uptime",
                value=f"`{' '.join(parts)}`",
                inline=True,
            )

        embed.set_footer(text="Novel Checking Bot")
        await ctx.send(embed=embed)

    @commands.command(name="serverinfo", aliases=["server", "guildinfo"])
    @commands.guild_only()
    async def server_info(self, ctx: commands.Context):
        """Display information about the current server."""
        guild = ctx.guild
        if guild is None:
            return

        embed = discord.Embed(
            title=f"{guild.name}",
            color=Colors.INFO,
            timestamp=discord.utils.utcnow(),
        )

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="Server ID",
            value=f"`{guild.id}`",
            inline=True,
        )
        embed.add_field(
            name="Owner",
            value=f"<@{guild.owner_id}>",
            inline=True,
        )
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, style="D"),
            inline=True,
        )

        if guild.member_count is not None:
            embed.add_field(
                name="Members",
                value=f"`{guild.member_count:,}`",
                inline=True,
            )

        embed.add_field(
            name="Text Channels",
            value=f"`{len(guild.text_channels):,}`",
            inline=True,
        )
        embed.add_field(
            name="Voice Channels",
            value=f"`{len(guild.voice_channels):,}`",
            inline=True,
        )
        embed.add_field(
            name="Categories",
            value=f"`{len(guild.categories):,}`",
            inline=True,
        )
        embed.add_field(
            name="Roles",
            value=f"`{len(guild.roles):,}`",
            inline=True,
        )
        embed.add_field(
            name="Boost Level",
            value=f"`{guild.premium_tier}`",
            inline=True,
        )

        if guild.description:
            embed.description = guild.description[:4000]

        embed.set_footer(text=f"Server ID: {guild.id}")
        await ctx.send(embed=embed)

    @commands.command(name="userinfo", aliases=["user", "whois"])
    async def user_info(
        self,
        ctx: commands.Context,
        *,
        member: discord.Member | discord.User | None = None,
    ):
        """Display information about a user."""
        target = member or ctx.author

        embed = discord.Embed(
            title=f"User Information: {target}",
            color=Colors.INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)

        embed.add_field(
            name="Username",
            value=str(target),
            inline=True,
        )
        embed.add_field(
            name="User ID",
            value=f"`{target.id}`",
            inline=True,
        )
        embed.add_field(
            name="Account Created",
            value=discord.utils.format_dt(target.created_at, style="F"),
            inline=False,
        )

        if isinstance(target, discord.Member):
            embed.add_field(
                name="Joined Server",
                value=(
                    discord.utils.format_dt(target.joined_at, style="F")
                    if target.joined_at
                    else "Unknown"
                ),
                inline=False,
            )

            roles = [
                role.mention
                for role in reversed(target.roles)
                if role != ctx.guild.default_role
            ] if ctx.guild else []

            role_text = ", ".join(roles)
            if len(role_text) > 1000:
                role_text = role_text[:997] + "..."

            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=role_text or "No roles",
                inline=False,
            )

            if target.guild_permissions.administrator:
                status = "Administrator"
            elif target.guild_permissions.manage_guild:
                status = "Server Manager"
            elif target.guild_permissions.manage_messages:
                status = "Message Moderator"
            else:
                status = "Member"

            embed.add_field(
                name="Server Permissions",
                value=status,
                inline=True,
            )

        embed.set_footer(text=f"Requested by {ctx.author}")
        await ctx.send(embed=embed)

    @commands.command(name="avatar", aliases=["pfp", "av"])
    async def avatar(
        self,
        ctx: commands.Context,
        *,
        member: discord.Member | discord.User | None = None,
    ):
        """Display a user's avatar."""
        target = member or ctx.author

        embed = discord.Embed(
            title=f"{target.display_name if isinstance(target, discord.Member) else target.name}'s Avatar",
            color=Colors.INFO,
        )
        embed.set_image(url=target.display_avatar.with_size(1024).url)
        embed.add_field(
            name="Links",
            value=(
                f"[PNG]({target.display_avatar.with_format('png').url}) · "
                f"[WebP]({target.display_avatar.with_format('webp').url})"
            ),
            inline=False,
        )

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))