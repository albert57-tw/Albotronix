"""Button polls. Buttons are DynamicItems, so they keep working after the bot restarts."""
import json
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.loops import guarded
from core import db
from core.embeds import MUTED, POLL as THEME, error_embed, make_embed, timestamp, truncate
from core.timeparse import parse_duration

NUMBERS = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
BAR_WIDTH = 10


async def load_poll(message_id: int):
    async with db.conn.execute('SELECT * FROM polls WHERE message_id = ?', (message_id,)) as cur:
        return await cur.fetchone()


async def tally(message_id: int, n_options: int) -> list[int]:
    counts = [0] * n_options
    async with db.conn.execute('SELECT option, COUNT(*) AS n FROM poll_votes WHERE message_id = ? GROUP BY option',
                               (message_id,)) as cur:
        for row in await cur.fetchall():
            counts[row['option']] = row['n']
    return counts


def poll_embed(poll, counts: list[int], closed: bool = False) -> discord.Embed:
    options = json.loads(poll['options'])
    total = sum(counts)
    best = max(counts) if total else -1
    lines = []
    for i, (option, count) in enumerate(zip(options, counts)):
        ratio = count / total if total else 0
        filled = round(ratio * BAR_WIDTH)
        bar = ('🟩' if closed and count == best else '🟦') * filled + '⬜' * (BAR_WIDTH - filled)
        crown = ' 👑' if closed and count == best else ''
        lines.append(f'{NUMBERS[i]} **{option}**{crown}\n{bar}　{count} 票（{ratio:.0%}）')
    when = (f'已於 {timestamp(poll["end_ts"], "R")} 結束' if closed
            else f'{timestamp(poll["end_ts"], "R")}截止 · 再按一次可以取消投票')
    return make_embed(f'📊 {poll["question"]}', '\n\n'.join(lines) + f'\n\n-# 共 {total} 票 · {when}',
                      theme=THEME, color=MUTED if closed else None)


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r'poll:(?P<option>\d+)'):
    def __init__(self, option: int, label: str = ''):
        super().__init__(discord.ui.Button(label=truncate(label, 80) or str(option + 1), emoji=NUMBERS[option],
                                           style=discord.ButtonStyle.secondary, custom_id=f'poll:{option}'))
        self.option = option

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match['option']), item.label or '')

    async def callback(self, interaction: discord.Interaction):
        poll = await load_poll(interaction.message.id)
        if not poll or poll['closed']:
            await interaction.response.send_message('這個投票已經結束了。', ephemeral=True)
            return
        options = json.loads(poll['options'])
        key = (interaction.message.id, interaction.user.id)
        async with db.conn.execute('SELECT option FROM poll_votes WHERE message_id = ? AND user_id = ?', key) as cur:
            existing = await cur.fetchone()
        if existing and existing['option'] == self.option:
            await db.conn.execute('DELETE FROM poll_votes WHERE message_id = ? AND user_id = ?', key)
            reply = f'已取消你投給「{options[self.option]}」的票。'
        else:
            await db.conn.execute('INSERT INTO poll_votes (message_id, user_id, option) VALUES (?, ?, ?) '
                                  'ON CONFLICT (message_id, user_id) DO UPDATE SET option = excluded.option',
                                  (*key, self.option))
            reply = f'✅ 你投給了「{options[self.option]}」，再按一次可以取消。'
        await db.conn.commit()
        counts = await tally(interaction.message.id, len(options))
        await interaction.response.edit_message(embed=poll_embed(poll, counts))
        await interaction.followup.send(reply, ephemeral=True)


class EndButton(discord.ui.DynamicItem[discord.ui.Button], template=r'poll:end'):
    def __init__(self):
        super().__init__(discord.ui.Button(label='結束投票', emoji='🛑', style=discord.ButtonStyle.danger,
                                           custom_id='poll:end'))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        poll = await load_poll(interaction.message.id)
        if not poll or poll['closed']:
            await interaction.response.send_message('這個投票已經結束了。', ephemeral=True)
            return
        if interaction.user.id != poll['author_id'] and not interaction.permissions.manage_messages:
            await interaction.response.send_message('只有發起投票的人可以提前結束。', ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.client.get_cog('Vote').close_poll(poll, ended_by=interaction.user)


class Vote(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(VoteButton, EndButton)
        self.close_expired.start()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(VoteButton, EndButton)
        self.close_expired.cancel()

    @app_commands.command()
    @app_commands.describe(question='投票題目', options='2–10 個選項，用 | 分隔，例如「火鍋 | 燒肉 | 拉麵」',
                           duration='投票時間，例如 30m、2h、1d（預設 1 小時）')
    async def poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200],
                   options: str, duration: str = '1h'):
        """發起一個按鈕投票"""
        choices = [o.strip() for o in options.split('|') if o.strip()]
        seconds = parse_duration(duration)
        if not 2 <= len(choices) <= 10:
            await interaction.response.send_message(embed=error_embed('請給 2–10 個選項，用 `|` 分隔。'),
                                                    ephemeral=True)
            return
        if not seconds or not 60 <= seconds <= 7 * 86400:
            await interaction.response.send_message(embed=error_embed('投票時間要介於 1 分鐘到 7 天之間。'),
                                                    ephemeral=True)
            return
        choices = [truncate(c, 100) for c in choices]
        end_ts = time.time() + seconds
        poll_row = {'question': question, 'options': json.dumps(choices), 'end_ts': end_ts}
        view = discord.ui.View(timeout=None)
        for i, choice in enumerate(choices):
            view.add_item(VoteButton(i, choice))
        view.add_item(EndButton())
        await interaction.response.send_message(embed=poll_embed(poll_row, [0] * len(choices)), view=view)
        message = await interaction.original_response()
        await db.conn.execute('INSERT INTO polls (message_id, channel_id, author_id, question, options, end_ts) '
                              'VALUES (?, ?, ?, ?, ?, ?)',
                              (message.id, message.channel.id, interaction.user.id, question, poll_row['options'], end_ts))
        await db.conn.commit()

    async def close_poll(self, poll, ended_by: discord.abc.User | None = None) -> None:
        poll = dict(poll)
        poll['end_ts'] = min(poll['end_ts'], time.time())
        await db.conn.execute('UPDATE polls SET closed = 1, end_ts = ? WHERE message_id = ?',
                              (poll['end_ts'], poll['message_id']))
        await db.conn.commit()
        counts = await tally(poll['message_id'], len(json.loads(poll['options'])))
        channel = self.bot.get_channel(poll['channel_id'])
        if not channel:
            return
        try:
            message = await channel.fetch_message(poll['message_id'])
            await message.edit(embed=poll_embed(poll, counts, closed=True), view=None)
            note = f'（由 {ended_by.mention} 提前結束）' if ended_by else ''
            await message.reply(f'📊 投票「{poll["question"]}」結束了{note}！',
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @tasks.loop(seconds=30)
    @guarded
    async def close_expired(self):
        async with db.conn.execute('SELECT * FROM polls WHERE closed = 0 AND end_ts <= ?', (time.time(),)) as cur:
            rows = await cur.fetchall()
        for poll in rows:
            await self.close_poll(poll)

    @close_expired.before_loop
    async def before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Vote(bot))
