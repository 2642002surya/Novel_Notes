import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
import uvicorn

from core.database import init_db
import web.app as web_module

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
WEB_PORT = int(os.getenv("PORT", "8000"))
WEB_HOST = os.getenv("HOST", "0.0.0.0")

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SentinelCore")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class SentinelBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or("i novel ", "I novel ", "I NOVEL "),
            case_insensitive=True,
            intents=intents,
            help_command=None,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="i novel help | Web Dashboard Active"
            ),
            status=discord.Status.online
        )

    async def setup_hook(self):
        logger.info("Initializing persistent database...")
        await init_db()

        logger.info("Loading dynamic Cog extensions...")
        cogs_dir = Path(__file__).parent / "cogs"
        for file in cogs_dir.glob("*.py"):
            if not file.name.startswith("__"):
                extension_name = f"cogs.{file.stem}"
                try:
                    await self.load_extension(extension_name)
                    logger.info("  ✓ Loaded Cog: %s", extension_name)
                except Exception as e:
                    logger.exception("  ✗ Failed to load cog %s", extension_name, exc_info=e)

    async def on_ready(self):
        logger.info("==========================================")
        logger.info("  Novel Sentinel is Online & Active!")
        logger.info("  Account: %s#%s (%s)", self.user.name, self.user.discriminator, self.user.id)
        logger.info("  Guilds Connected: %d", len(self.guilds))
        logger.info("  Web Dashboard: http://localhost:%d", WEB_PORT)
        logger.info("==========================================")

bot = SentinelBot()
web_module.discord_bot = bot

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(
            title="🚫 Access Denied",
            description=f"You need `{', '.join(error.missing_permissions)}` permission to execute this.",
            color=discord.Color.from_rgb(237, 66, 69)
        )
        return await ctx.reply(embed=embed, mention_author=False)

    if isinstance(error, commands.BotMissingPermissions):
        embed = discord.Embed(
            title="⚠️ Bot Missing Permissions",
            description=f"I require `{', '.join(error.missing_permissions)}` permissions in this channel.",
            color=discord.Color.from_rgb(237, 66, 69)
        )
        return await ctx.reply(embed=embed, mention_author=False)

    logger.error("Unhandled command error: %s", error, exc_info=error)

async def main():
    if not TOKEN:
        logger.critical("DISCORD_TOKEN missing in environment.")
        sys.exit(1)

    # Uvicorn web server running on the same loop
    config = uvicorn.Config(web_module.app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
    server = uvicorn.Server(config)

    # Wrap the runner in a task group to handle clean cancellation
    bot_task = asyncio.create_task(bot.start(TOKEN))
    web_task = asyncio.create_task(server.serve())

    try:
        await asyncio.gather(bot_task, web_task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("Initiating graceful shutdown...")
        # 1. Stop Uvicorn server
        server.should_exit = True
        
        # 2. Close Discord connection properly before the loop closes
        if not bot.is_closed():
            await bot.close()
            
        # 3. Allow pending tasks and thread heartbeats to complete
        await asyncio.sleep(0.5)
        logger.info("Sentinel shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass