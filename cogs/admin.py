from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

NON_COG_FILES = {"__init__.py", "novel_database.py"}


class Admin(commands.Cog):
    """Administrative commands for managing the bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._extension_lock = asyncio.Lock()

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()

        if not isinstance(ctx.author, discord.Member):
            raise commands.CheckFailure()

        if not ctx.author.guild_permissions.administrator:
            raise commands.MissingPermissions(["administrator"])

        return True

    def get_available_cogs(self) -> list[str]:
        base_dir = Path(getattr(self.bot, "base_dir", Path.cwd()))
        cogs_dir = base_dir / "cogs"

        if not cogs_dir.is_dir():
            return []

        return sorted(
            file.stem
            for file in cogs_dir.glob("*.py")
            if file.name not in NON_COG_FILES
            and not file.name.startswith("_")
        )

    def resolve_extension(self, name: str) -> str:
        name = name.strip()

        if name.startswith("cogs."):
            name = name[5:]

        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError("Invalid extension name.")

        available = self.get_available_cogs()

        if name.lower() == "all":
            raise ValueError("Use the `all` option only with reloadall.")

        match = next(
            (item for item in available if item.lower() == name.lower()),
            None,
        )

        if match is None:
            raise ValueError(
                "Unknown extension. Available extensions: "
                + ", ".join(available)
            )

        return f"cogs.{match}"

    @commands.command(name="ping")
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def ping(self, ctx: commands.Context):
        """Check bot responsiveness."""
        start = time.perf_counter()
        message = await ctx.send("Checking latency...")
        response_ms = (time.perf_counter() - start) * 1000
        gateway_ms = self.bot.latency * 1000

        await message.edit(
            content=(
                f"**Gateway:** `{gateway_ms:.0f} ms`\n"
                f"**Response:** `{response_ms:.0f} ms`"
            )
        )

    @commands.command(name="cogs")
    async def extension_menu(self, ctx: commands.Context):
        """Show available extensions and their status."""
        available = self.get_available_cogs()

        if not available:
            await ctx.send("No extensions were found.")
            return

        embed = discord.Embed(
            title="Bot Extensions",
            color=discord.Color.blurple(),
        )

        lines = []
        for name in available:
            extension = f"cogs.{name}"
            status = (
                "Loaded"
                if extension in self.bot.extensions
                else "Not loaded"
            )
            lines.append(f"`{extension}` — **{status}**")

        embed.description = "\n".join(lines)[:4000]
        await ctx.send(embed=embed)

    @commands.command(name="reload")
    async def reload_cog(
        self,
        ctx: commands.Context,
        *,
        cog_name: str,
    ):
        """Reload one extension."""
        try:
            extension = self.resolve_extension(cog_name)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        async with self._extension_lock:
            try:
                await self.bot.reload_extension(extension)
                logger.info(
                    "%s reloaded extension %s",
                    ctx.author,
                    extension,
                )
                await ctx.send(f"Successfully reloaded `{extension}`.")
            except Exception:
                logger.exception("Failed to reload extension %s", extension)
                await ctx.send(
                    f"Failed to reload `{extension}`. Check the bot logs."
                )

    @commands.command(name="load")
    async def load_cog(
        self,
        ctx: commands.Context,
        *,
        cog_name: str,
    ):
        """Load one extension."""
        try:
            extension = self.resolve_extension(cog_name)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        async with self._extension_lock:
            if extension in self.bot.extensions:
                await ctx.send(f"`{extension}` is already loaded.")
                return

            try:
                await self.bot.load_extension(extension)
                logger.info(
                    "%s loaded extension %s",
                    ctx.author,
                    extension,
                )
                await ctx.send(f"Successfully loaded `{extension}`.")
            except Exception:
                logger.exception("Failed to load extension %s", extension)
                await ctx.send(
                    f"Failed to load `{extension}`. Check the bot logs."
                )

    @commands.command(name="unload")
    async def unload_cog(
        self,
        ctx: commands.Context,
        *,
        cog_name: str,
    ):
        """Unload one extension."""
        try:
            extension = self.resolve_extension(cog_name)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        if extension == "cogs.admin":
            await ctx.send("The admin extension cannot unload itself.")
            return

        async with self._extension_lock:
            if extension not in self.bot.extensions:
                await ctx.send(f"`{extension}` is not loaded.")
                return

            try:
                await self.bot.unload_extension(extension)
                logger.info(
                    "%s unloaded extension %s",
                    ctx.author,
                    extension,
                )
                await ctx.send(f"Successfully unloaded `{extension}`.")
            except Exception:
                logger.exception("Failed to unload extension %s", extension)
                await ctx.send(
                    f"Failed to unload `{extension}`. Check the bot logs."
                )

    @commands.command(name="reloadall")
    async def reload_all(self, ctx: commands.Context):
        """Reload all loaded extensions except this admin extension."""
        extensions = sorted(
            extension
            for extension in self.bot.extensions
            if extension != "cogs.admin"
        )

        if not extensions:
            await ctx.send("No other loaded extensions were found.")
            return

        results = []

        async with self._extension_lock:
            for extension in extensions:
                try:
                    await self.bot.reload_extension(extension)
                    results.append(f"Success: `{extension}`")
                except Exception:
                    logger.exception(
                        "Failed to reload extension %s",
                        extension,
                    )
                    results.append(f"Failed: `{extension}`")

        description = "\n".join(results)
        if len(description) > 4000:
            description = description[:3990] + "\n..."

        embed = discord.Embed(
            title="Extension Reload Results",
            description=description,
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="sync")
    async def sync_commands(self, ctx: commands.Context):
        """Synchronize application commands with Discord."""
        try:
            synced = await self.bot.tree.sync()
            await ctx.send(
                f"Successfully synchronized `{len(synced)}` application command(s)."
            )
        except Exception:
            logger.exception("Failed to synchronize application commands")
            await ctx.send(
                "Failed to synchronize application commands. Check the bot logs."
            )

    @commands.command(name="shutdown")
    async def shutdown(self, ctx: commands.Context):
        """Shut down the bot."""
        await ctx.send("Shutting down the bot...")
        logger.warning("Shutdown requested by %s", ctx.author)
        await self.bot.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))