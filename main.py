from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Imported after load_dotenv so modules that read env vars at import time see them.
from novelbot.embeds import error_embed  # noqa: E402

NON_COG_FILES = {"__init__.py", "novel_database.py"}
PREFIX = os.getenv("COMMAND_PREFIX", "i novel ") or "i novel "
if PREFIX[-1].isalnum():
    # Word prefixes need a separating space; .env loaders strip an unquoted trailing one.
    PREFIX += " "

logger = logging.getLogger("NovelGuard")


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file_handler])
    logging.getLogger("discord.http").setLevel(logging.WARNING)


def get_prefix(bot: commands.Bot, message: discord.Message):
    """Case-insensitive text prefix (default 'i novel ') plus @mention."""
    content = message.content or ""
    if content[: len(PREFIX)].lower() == PREFIX.lower():
        return [content[: len(PREFIX)]]
    return commands.when_mentioned(bot, message)


class NovelBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # also enable in the Developer Portal
        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,  # custom help lives in cogs/help.py
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.base_dir = BASE_DIR
        self.started_at = discord.utils.utcnow()

    async def setup_hook(self) -> None:
        cogs_dir = BASE_DIR / "cogs"
        for file in sorted(cogs_dir.glob("*.py")):
            if file.name in NON_COG_FILES or file.name.startswith("_"):
                continue
            extension = f"cogs.{file.stem}"
            try:
                await self.load_extension(extension)
                logger.info("Loaded extension %s", extension)
            except Exception:
                logger.exception("Failed to load extension %s", extension)

    async def close(self) -> None:
        service = getattr(self, "novel_service", None)
        if service is not None:
            await service.close()  # releases the shared HTTP session
        await super().close()

    async def on_ready(self) -> None:
        logger.info(
            "Logged in as %s (%s) in %d server(s)",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandInvokeError):
            error = error.original or error  # type: ignore[assignment]

        if isinstance(error, commands.MissingPermissions):
            message = "You do not have permission to use this command."
        elif isinstance(error, commands.NoPrivateMessage):
            message = "This command can only be used in a server."
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"Slow down. Try again in {error.retry_after:.0f}s."
        elif isinstance(error, commands.MissingRequiredArgument):
            message = f"Missing argument: `{error.param.name}`."
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            message = "Invalid argument. Check the command usage with the help menu."
        elif isinstance(error, commands.CheckFailure):
            message = "You cannot use this command here."
        else:
            logger.error("Unhandled error in command %s", ctx.command, exc_info=error)
            message = "Something went wrong. Check the bot logs."

        try:
            await ctx.send(embed=error_embed(message))
        except discord.HTTPException:
            # e.g. the bot may not be allowed to speak in this channel.
            logger.warning("Could not send error message in channel %s", ctx.channel.id)


def main() -> None:
    setup_logging()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "YOUR_DISCORD_BOT_TOKEN":
        logger.critical("DISCORD_TOKEN is missing. Copy .env.example to .env and add your token.")
        sys.exit(1)
    NovelBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
