import logging
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import errors, types

import config
from core import FeatureDisabled
from core.embeds import AI as THEME, chunk_text, error_embed, make_embed, note_embed, truncate
from cogs.subscriptions import TOPICS

log = logging.getLogger(__name__)

PERSONA = (
    'You are Albotronix, a friendly and concise assistant living in a Discord server of students who '
    'enjoy CTFs, competitive programming and cybersecurity. Reply in Traditional Chinese (繁體中文, Taiwan) '
    'unless the user writes in another language. Use Discord markdown. Keep answers short unless asked '
    'for detail. For security questions, help with learning, CTFs and defense.'
)
MANUAL_INTRO = (
    'Below is your own feature list, generated from your registered slash commands. When someone asks what '
    'you can do or how to use something, answer from it: name the exact command and its options, and never '
    'invent commands or features that are not listed.'
)
SUMMARY_PROMPT = ('Summarize this Discord conversation in Traditional Chinese (繁體中文) as a few bullet points: '
                  'main topics, decisions, and open questions. Mention people by their display names.\n\n')


class RateLimiter:
    def __init__(self, limit: int, per: float = 60):
        self.limit, self.per = limit, per
        self.calls: dict[int, deque[float]] = defaultdict(deque)

    def retry_after(self, user_id: int) -> float:
        """0 if allowed now (and records the call), otherwise seconds to wait."""
        now = time.monotonic()
        calls = self.calls[user_id]
        while calls and now - calls[0] > self.per:
            calls.popleft()
        if len(calls) >= self.limit:
            return self.per - (now - calls[0])
        calls.append(now)
        return 0


class AI(commands.Cog):
    ai = app_commands.Group(name='ai', description='Gemini AI 聊天')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = genai.Client(api_key=config.GEMINI_API_KEY)
        self.config: types.GenerateContentConfig | None = None  # built on first use, once every cog is loaded
        self.chats = {}  # channel id -> AsyncChat
        self.limiter = RateLimiter(config.AI_RATE_LIMIT)

    def _chat(self, channel_id: int):
        chat = self.chats.get(channel_id)
        if chat:
            # Keep only the last N exchanges so prompts (and token usage) stay small.
            history = chat.get_history()
            if len(history) > config.AI_HISTORY_TURNS * 2:
                chat = None
                history = history[-config.AI_HISTORY_TURNS * 2:]
        else:
            history = []
        if chat is None:
            if self.config is None:
                self.config = types.GenerateContentConfig(system_instruction=f'{PERSONA}\n\n{self.manual()}')
            chat = self.client.aio.chats.create(model=config.GEMINI_MODEL, config=self.config, history=history)
            self.chats[channel_id] = chat
        return chat

    def manual(self) -> str:
        """What the bot can do, from the live command tree plus the scheduled posts, for the system prompt."""
        lines = [MANUAL_INTRO, '', 'Slash commands:']
        for top in sorted(self.bot.tree.get_commands(), key=lambda c: c.name):
            commands_ = top.commands if isinstance(top, app_commands.Group) else [top]
            if isinstance(top, app_commands.Group):
                lines.append(f'/{top.name} — {top.description}')
            for cmd in commands_:
                params = '; '.join(f'{p.display_name}{"" if p.required else " (optional)"}: {p.description}'
                                   for p in cmd.parameters)
                lines.append(f'  /{cmd.qualified_name} — {cmd.description}' + (f' [{params}]' if params else ''))
        lines += ['', 'Also: mention @Albotronix in a message to chat with you (same memory as /ai ask).',
                  '', 'Scheduled posts (Taiwan time), sent to their channels without pinging anyone:',
                  f'  {config.CTF_NOTIFY_TIME:%H:%M} CTFs in the next 3 days',
                  f'  {config.WEATHER_TIME:%H:%M} weather forecast for {config.WEATHER_CITY}',
                  f'  {config.LEETCODE_TIME:%H:%M} LeetCode daily challenge',
                  f'  {config.CSES_TIME:%H:%M} CSES daily problem with study hints',
                  f'  {config.SECURITY_TIME:%H:%M} new CISA KEV entries and critical CVEs',
                  f'  Sunday from {config.OIL_TIME:%H:%M}: next week\'s CPC fuel prices',
                  '  any time: significant earthquake reports; THSR / TRA / metro service disruptions',
                  '', 'Pings: only the "starts in 1 hour" reminders ping, and only people who subscribed to that '
                  'topic with /notify settings (or the /notify panel buttons):']
        lines += [f'  {emoji} {name}: {desc}' for emoji, name, desc in TOPICS.values()]
        return '\n'.join(lines)

    async def _reply(self, channel_id: int, user: discord.abc.User, text: str) -> str:
        chat = self._chat(channel_id)
        response = await chat.send_message(f'[{user.display_name}] {text}')
        return response.text or '（AI 沒有回應，可能被安全過濾擋下了，換個問法試試。）'

    @staticmethod
    def explain(error: Exception) -> str:
        if isinstance(error, errors.APIError) and error.code == 429:
            return 'Gemini 的免費額度暫時用完了，請過一分鐘（或明天）再試。'
        if isinstance(error, errors.APIError) and error.code in (400, 403):
            return f'Gemini 拒絕了這個請求（{error.code}），請 bot 管理者檢查 GEMINI_API_KEY / GEMINI_MODEL。'
        return f'Gemini 發生錯誤：`{type(error).__name__}`'

    @ai.command(name='ask')
    @app_commands.describe(question='想問什麼都可以')
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 1500]):
        """問 AI 問題（會記得這個頻道最近的對話）"""
        if wait := self.limiter.retry_after(interaction.user.id):
            await interaction.response.send_message(embed=error_embed(f'太快了！請 {wait:.0f} 秒後再試。'),
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        try:
            answer = await self._reply(interaction.channel_id, interaction.user, question)
        except Exception as e:
            log.warning('Gemini /ai ask failed', exc_info=True)
            await interaction.followup.send(embed=error_embed(self.explain(e)))
            return
        for i, chunk in enumerate(chunk_text(answer, 4000)):
            embed = make_embed(truncate(question.replace('\n', ' '), 256) if i == 0 else None, chunk, theme=THEME)
            if i == 0:
                embed.set_author(name=f'{interaction.user.display_name} 問', icon_url=interaction.user.display_avatar.url)
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @ai.command(name='reset')
    async def reset(self, interaction: discord.Interaction):
        """清除 AI 在這個頻道的對話記憶"""
        self.chats.pop(interaction.channel_id, None)
        await interaction.response.send_message(embed=note_embed('🧹 已清除這個頻道的對話記憶，重新開始聊吧！', THEME))

    @ai.command(name='summarize')
    @app_commands.describe(count='要摘要最近幾則訊息（10–200）')
    async def summarize(self, interaction: discord.Interaction, count: app_commands.Range[int, 10, 200] = 50):
        """用 AI 摘要這個頻道最近的對話"""
        if not config.MESSAGE_CONTENT_INTENT:
            await interaction.response.send_message(embed=error_embed(
                '/ai summarize 需要 Message Content 權限：請 bot 管理者在 Developer Portal 開啟，並設定 '
                'MESSAGE_CONTENT_INTENT=1。'), ephemeral=True)
            return
        # The bot reads the history, so don't let it reveal older messages the user isn't allowed to see.
        if not interaction.permissions.read_message_history:
            await interaction.response.send_message(embed=error_embed('你在這個頻道沒有「讀取訊息歷史」權限。'),
                                                    ephemeral=True)
            return
        if wait := self.limiter.retry_after(interaction.user.id):
            await interaction.response.send_message(embed=error_embed(f'太快了！請 {wait:.0f} 秒後再試。'),
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        lines = [f'{m.author.display_name}: {m.clean_content}'
                 async for m in interaction.channel.history(limit=count) if m.clean_content and not m.author.bot]
        if not lines:
            await interaction.followup.send(embed=note_embed('最近沒有可以摘要的訊息。', THEME))
            return
        try:
            response = await self.client.aio.models.generate_content(
                model=config.GEMINI_MODEL, contents=SUMMARY_PROMPT + '\n'.join(reversed(lines)))
        except Exception as e:
            log.warning('Gemini summarize failed', exc_info=True)
            await interaction.followup.send(embed=error_embed(self.explain(e)))
            return
        for i, chunk in enumerate(chunk_text(response.text or '（沒有內容）', 4000)):
            embed = make_embed(f'📝 最近 {len(lines)} 則訊息摘要' if i == 0 else None, chunk, theme=THEME, stamp=True)
            await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Messages that mention the bot include their content even without the Message Content intent.
        if message.author.bot or not self.bot.user or self.bot.user not in message.mentions:
            return
        text = message.content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '').strip()
        if not text:
            return
        if wait := self.limiter.retry_after(message.author.id):
            await message.reply(f'太快了！請 {wait:.0f} 秒後再試。', mention_author=False)
            return
        async with message.channel.typing():
            try:
                answer = await self._reply(message.channel.id, message.author, text)
            except Exception as e:
                log.warning('Gemini mention failed', exc_info=True)
                answer = '❌ ' + self.explain(e)
        chunks = chunk_text(answer)
        await message.reply(chunks[0], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        for chunk in chunks[1:]:
            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    if not config.GEMINI_API_KEY:
        raise FeatureDisabled('GEMINI_API_KEY not set')
    await bot.add_cog(AI(bot))
