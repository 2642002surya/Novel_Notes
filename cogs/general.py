import datetime
import time
import discord
from discord.ext import commands

COLOR_PRIMARY = discord.Color.from_rgb(88, 101, 242)
COLOR_SUCCESS = discord.Color.from_rgb(87, 242, 135)
COLOR_WARNING = discord.Color.from_rgb(254, 231, 92)

class DynamicHelpSelect(discord.ui.Select):
    """Dropdown menu to switch documentation categories seamlessly."""
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self.ctx = ctx
        options = []
        for name, cog in bot.cogs.items():
            cmds = [c for c in cog.get_commands() if not c.hidden]
            if cmds:
                options.append(discord.SelectOption(
                    label=name,
                    description=f"{len(cmds)} commands available",
                    emoji="📂"
                ))

        super().__init__(
            placeholder="Select a category to view commands...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("Only the command runner can change this view.", ephemeral=True)

        selected_cog_name = self.values[0]
        cog = self.bot.get_cog(selected_cog_name)
        if not cog:
            return await interaction.response.send_message("Category not found.", ephemeral=True)

        embed = discord.Embed(
            title=f"📂 Category: {selected_cog_name}",
            description=cog.description or "No category description.",
            color=COLOR_PRIMARY,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_footer(text=f"Requested by {self.ctx.author}", icon_url=self.ctx.author.display_avatar.url)

        for cmd in cog.get_commands():
            if cmd.hidden:
                continue
            sig = f"{self.ctx.prefix}{cmd.name} {cmd.signature}".strip()
            desc = cmd.short_doc or "No details."
            aliases = f"\n*Aliases:* `{', '.join(cmd.aliases)}`" if cmd.aliases else ""
            embed.add_field(name=f"`{sig}`", value=f"{desc}{aliases}", inline=False)

        await interaction.response.edit_message(embed=embed)

class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        super().__init__(timeout=180)
        self.add_item(DynamicHelpSelect(bot, ctx))

class General(commands.Cog, name="General & Administration"):
    """Core utilities, latency telemetry, and channel management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.uptime_start = time.time()

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context):
        """Checks gateway connection latency and HTTP roundtrip."""
        t_start = time.monotonic()
        msg = await ctx.reply("📡 Measuring socket connection...", mention_author=False)
        t_end = time.monotonic()

        http_latency = round((t_end - t_start) * 1000)
        ws_latency = round(self.bot.latency * 1000)

        embed = discord.Embed(
            title="⚡ Network Performance",
            color=COLOR_SUCCESS if ws_latency < 160 else COLOR_WARNING,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(name="WebSocket Heartbeat", value=f"```fix\n{ws_latency} ms\n```", inline=True)
        embed.add_field(name="HTTP Roundtrip", value=f"```fix\n{http_latency} ms\n```", inline=True)
        uptime_delta = str(datetime.timedelta(seconds=int(time.time() - self.uptime_start)))
        embed.add_field(name="Bot Uptime", value=f"```yaml\n{uptime_delta}\n```", inline=False)
        embed.set_footer(text="Novel Sentinel Telemetry", icon_url=self.bot.user.display_avatar.url)

        await msg.edit(content=None, embed=embed)

    @commands.command(name="purge", aliases=["clean"])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(self, ctx: commands.Context, limit: int = 50):
        """Purges recent messages in current channel with instant visual confirmation."""
        if limit < 1 or limit > 500:
            return await ctx.reply("⚠️ Specify an amount between 1 and 500.", mention_author=False)

        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=limit)

        embed = discord.Embed(
            title="🧹 Channel Cleanup Complete",
            description=f"Successfully wiped **{len(deleted)}** messages from {ctx.channel.mention}.",
            color=COLOR_SUCCESS
        )
        embed.set_footer(text="This notification will auto-delete in 5 seconds.")
        notif = await ctx.send(embed=embed)
        await notif.delete(delay=5)

    @commands.command(name="help")
    async def help(self, ctx: commands.Context):
        """Displays interactive command center and cog architecture overview."""
        embed = discord.Embed(
            title="📖 Novel Sentinel • Command Center",
            description=(
                f"Prefix: `{ctx.prefix}`\n\n"
                "Welcome to the production novel moderation suite. Select a category below "
                "to inspect commands, signatures, and documentation.\n"
                "────────────────────────────────────────────"
            ),
            color=COLOR_PRIMARY,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(
            name="Available Modules",
            value="• **General & Administration**: Ping, Purge, Help\n• **Novel Scanner**: Scan, Deduplication, Link Management",
            inline=False
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Use the dropdown menu to jump into detailed views.")

        view = HelpView(self.bot, ctx)
        await ctx.reply(embed=embed, view=view, mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))