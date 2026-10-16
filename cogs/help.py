
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from novelbot.embeds import Colors, error_embed

logger = logging.getLogger("NovelGuard.Help")

COLOR = Colors.INFO
VIEW_TIMEOUT = 90
MAX_OPTIONS = 25


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, help_view: "HelpView") -> None:
        # Not named `parent`: newer discord.py versions use that name on UI items.
        self.help_view = help_view

        options = [
            discord.SelectOption(
                label="All Commands",
                value="all",
                description="Browse every available command.",
                emoji="📚",
            ),
            discord.SelectOption(
                label="Novel Tracker",
                value="novel_tracker",
                description="Novel indexing, duplicate detection, and search.",
                emoji="📖",
            ),
            discord.SelectOption(
                label="Library & Tracking",
                value="library",
                description="Track novels, browse your library, release alerts.",
                emoji="📚",
            ),
            discord.SelectOption(
                label="Cleaner",
                value="cleaner",
                description="Message cleanup and channel management.",
                emoji="🧹",
            ),
            discord.SelectOption(
                label="Administration",
                value="admin",
                description="Bot diagnostics and extension management.",
                emoji="⚙️",
            ),
            discord.SelectOption(
                label="General",
                value="general",
                description="General server and bot information.",
                emoji="ℹ️",
            ),
        ]

        super().__init__(
            placeholder="Choose a command category…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.show_category(
            interaction,
            self.values[0],
        )


class HelpView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        author_id: int,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.add_item(HelpCategorySelect(self))

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This help menu belongs to another user. Run the help command "
                "to open your own menu.",
                ephemeral=True,
            )
            return False

        return True

    def _command_categories(self) -> dict[str, list[commands.Command]]:
        categories: dict[str, list[commands.Command]] = {
            "novel_tracker": [],
            "library": [],
            "cleaner": [],
            "admin": [],
            "general": [],
        }

        for command in self.bot.walk_commands():
            if command.hidden:
                continue

            cog_name = (
                command.cog.qualified_name.lower()
                if command.cog
                else ""
            )

            if "novellibrary" in cog_name:
                categories["library"].append(command)
            elif "noveltracker" in cog_name or "novel_tracker" in cog_name:
                categories["novel_tracker"].append(command)
            elif "cleaner" in cog_name:
                categories["cleaner"].append(command)
            elif "admin" in cog_name:
                categories["admin"].append(command)
            else:
                categories["general"].append(command)

        for commands_list in categories.values():
            commands_list.sort(key=lambda command: command.qualified_name)

        return categories

    @staticmethod
    def _format_command(command: commands.Command) -> str:
        prefix = "i novel "
        signature = f" {command.signature}" if command.signature else ""
        description = command.short_doc or command.help or "No description provided."
        description = description.splitlines()[0][:180]

        return (
            f"**`{prefix}{command.qualified_name}{signature}`**\n"
            f"{description}"
        )

    def _build_embed(self, category: str) -> discord.Embed:
        categories = self._command_categories()

        if category == "all":
            title = "NovelGuard — Command Reference"
            selected = [
                command
                for commands_list in categories.values()
                for command in commands_list
            ]
        else:
            selected = categories.get(category, [])
            title = {
                "novel_tracker": "Novel Tracker Commands",
                "library": "Library & Tracking Commands",
                "cleaner": "Cleaner Commands",
                "admin": "Administration Commands",
                "general": "General Commands",
            }.get(category, "Command Reference")

        embed = discord.Embed(
            title=title,
            description=(
                "Use the command prefix `i novel ` followed by a command.\n"
                "Select a category from the menu below."
            ),
            color=COLOR,
            timestamp=discord.utils.utcnow(),
        )

        if not selected:
            embed.add_field(
                name="No commands available",
                value="No commands are currently registered in this category.",
                inline=False,
            )
        else:
            # Keep total embed size below Discord's 6000-character limit.
            current_value = ""
            field_number = 1

            for command in selected:
                entry = self._format_command(command)

                if len(current_value) + len(entry) + 2 > 950:
                    embed.add_field(
                        name=f"Commands {field_number}",
                        value=current_value,
                        inline=False,
                    )
                    field_number += 1
                    current_value = ""

                current_value += ("\n\n" if current_value else "") + entry

            if current_value:
                embed.add_field(
                    name=f"Commands {field_number}",
                    value=current_value,
                    inline=False,
                )

        embed.set_footer(
            text=f"{self.bot.user.name if self.bot.user else 'NovelGuard'} • {len(selected)} command(s)"
        )
        return embed

    async def show_category(
        self,
        interaction: discord.Interaction,
        category: str,
    ) -> None:
        embed = self._build_embed(category)

        await interaction.response.edit_message(
            embed=embed,
            view=self,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                item.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Help menu expired before it could be disabled.")


class Help(commands.Cog):
    """Interactive command documentation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(
        name="help",
        aliases=["commands", "cmds"],
    )
    async def help_command(self, ctx: commands.Context) -> None:
        """Show the interactive command reference."""
        view = HelpView(self.bot, ctx.author.id)

        message = await ctx.send(
            embed=view._build_embed("all"),
            view=view,
        )
        view.message = message

    @commands.command(name="commandinfo", aliases=["cmdinfo"])
    async def command_info(
        self,
        ctx: commands.Context,
        *,
        command_name: str,
    ) -> None:
        """Display detailed usage information for a command."""
        command = self.bot.get_command(command_name.strip())

        if command is None or command.hidden:
            await ctx.reply(
                embed=error_embed(
                    "Use `i novel help` to browse commands.", title="Command not found"
                ),
                mention_author=False,
            )
            return

        embed = discord.Embed(
            title=f"Command: {command.qualified_name}",
            description=command.help or command.short_doc or "No description provided.",
            color=COLOR,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="Usage",
            value=f"`i novel {command.qualified_name}"
            f"{' ' + command.signature if command.signature else ''}`",
            inline=False,
        )

        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{alias}`" for alias in command.aliases),
                inline=False,
            )

        if command.parent:
            embed.add_field(
                name="Parent command",
                value=f"`{command.parent.qualified_name}`",
                inline=False,
            )

        if command.cog:
            embed.add_field(
                name="Module",
                value=command.cog.qualified_name,
                inline=True,
            )

        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
