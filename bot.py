import asyncio
import logging
import logging.handlers
import os

import discord
from discord import app_commands
from discord.ext import commands

import config
from core import FeatureDisabled, UserFacingError, db, http, icons
from core.embeds import error_embed

log = logging.getLogger('albotronix')

EXTENSIONS = [
    'cogs.general',
    'cogs.fun',
    'cogs.thsr',
    'cogs.tra',
    'cogs.metro',
    'cogs.bus',
    'cogs.weather',
    'cogs.events',
    'cogs.ctf',
    'cogs.contests',
    'cogs.info',
    'cogs.reminder',
    'cogs.vote',
    'cogs.ai',
    'cogs.subscriptions',
]


class Albotronix(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = config.MESSAGE_CONTENT_INTENT
        # Never @everyone / @here or roles unless a message opts in (the contest reminders name their role),
        # so user-supplied text can't make the bot mass-ping.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.guild_object = discord.Object(id=config.GUILD_ID)

    async def on_message(self, message: discord.Message) -> None:
        # Slash commands only. The default would parse "@Albotronix hi" as a prefix command and log
        # CommandNotFound for every AI mention; cog listeners still receive the message.
        pass

    async def setup_hook(self) -> None:
        await http.start()
        await db.init(config.DB_PATH)
        try:
            await icons.sync(self)  # upload any new assets/emojis/*.png as application emojis
        except Exception:
            log.warning('Could not sync application emojis; using text instead', exc_info=True)
        self.tree.on_error = self.on_app_command_error
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
            except commands.ExtensionFailed as e:
                if isinstance(e.original, FeatureDisabled):
                    log.warning('%s disabled: %s', ext, e.original)
                else:
                    log.exception('Failed to load %s', ext)
            except Exception:
                # One broken feature must not take the whole bot down.
                log.exception('Failed to load %s', ext)
        self.tree.copy_global_to(guild=self.guild_object)
        synced = await self.tree.sync(guild=self.guild_object)
        log.info('Synced %d commands to guild %s', len(synced), config.GUILD_ID)

    async def close(self) -> None:
        await super().close()
        await http.close()
        await db.close()

    async def on_ready(self) -> None:
        log.info('Logged in as %s (ID: %s)', self.user, self.user.id)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Answer clicks on buttons whose view no longer exists (e.g. created before a restart).

        discord.py silently drops those, so the user would only see "didn't respond in time".
        Discord needs a reply within 3 seconds; any live view has answered by 2.5.
        """
        if interaction.type is not discord.InteractionType.component:
            return
        await asyncio.sleep(2.5)
        if interaction.response.is_done():
            return
        notice = '⌛ 這些按鈕已失效（bot 重新啟動過），請重新執行一次指令。'
        try:
            if interaction.message and interaction.message.flags.components_v2:
                # In a layout message the view *is* the content; removing it would blank the message.
                await interaction.response.send_message(notice, ephemeral=True)
            else:
                await interaction.response.edit_message(view=None)
                await interaction.followup.send(notice, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f'太快了！請 {error.retry_after:.0f} 秒後再試。'
        elif isinstance(error, app_commands.CheckFailure):
            message = str(error) or '你沒有使用這個指令的權限。'
        elif isinstance(getattr(error, 'original', None), UserFacingError):
            message = str(error.original)
        else:
            original = getattr(error, 'original', error)
            log.error('Error in /%s', interaction.command and interaction.command.qualified_name,
                      exc_info=original)
            message = f'發生錯誤：`{type(original).__name__}`，已記錄在 log。'
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(message), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    if not config.TOKEN or not config.GUILD_ID:
        raise SystemExit('TOKEN and GUILD_ID must be set in .env (see .env.example).')
    discord.utils.setup_logging(level=logging.INFO)  # console
    # Also keep a log file: 5 MB per file, the 5 most recent rotated files are kept.
    os.makedirs(config.LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(config.LOG_DIR, 'albotronix.log'), maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}',
                                                '%Y-%m-%d %H:%M:%S', style='{'))
    logging.getLogger().addHandler(file_handler)
    Albotronix().run(config.TOKEN, log_handler=None)


if __name__ == '__main__':
    main()
