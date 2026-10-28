import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.loops import guarded
from core import db
from core.embeds import REMINDER as THEME, error_embed, make_embed, note_embed, timestamp, truncate
from core.timeparse import parse_duration, parse_when

MAX_PER_USER = 25


def fmt_span(seconds: float) -> str:
    """5400 -> '1 小時 30 分', 1500 -> '25 分鐘', 90 -> '1 分 30 秒'."""
    seconds = round(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h} 小時' + (f' {m} 分' if m else '')
    if m:
        return f'{m} 分' + (f' {s} 秒' if s else '鐘')
    return f'{s} 秒'


def describe(row) -> str:
    """One-line description for lists and autocomplete."""
    if row['kind'] == 'timer':
        span = fmt_span(row['due_ts'] - row['created_ts'])
        return f'⏱️ {row["message"]}（{span}計時）' if row['message'] else f'⏱️ {span}計時'
    return row['message']


class Reminder(commands.Cog):
    remind = app_commands.Group(name='remind', description='提醒（bot 重開也不會遺失）')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_due.start()

    async def cog_unload(self) -> None:
        self.check_due.cancel()

    async def _create(self, interaction: discord.Interaction, due_ts: float, message: str, kind: str) -> None:
        async with db.conn.execute('SELECT COUNT(*) AS n FROM reminders WHERE user_id = ?',
                                   (interaction.user.id,)) as cur:
            if (await cur.fetchone())['n'] >= MAX_PER_USER:
                await interaction.response.send_message(
                    embed=error_embed(f'你已經有 {MAX_PER_USER} 個提醒了，先取消一些吧。'), ephemeral=True)
                return
        now = time.time()
        # The reminder is posted by the bot later: where the user can't send messages (e.g. an announcement
        # channel) it goes to their DMs instead (channel 0 never resolves, so delivery falls back to a DM).
        channel_id = interaction.channel_id if interaction.permissions.send_messages else 0
        cur = await db.conn.execute(
            'INSERT INTO reminders (user_id, channel_id, due_ts, message, created_ts, kind) VALUES (?, ?, ?, ?, ?, ?)',
            (interaction.user.id, channel_id, due_ts, message, now, kind))
        await db.conn.commit()
        when = f'{timestamp(due_ts, "f")}（{timestamp(due_ts, "R")}）'
        if kind == 'timer':
            embed = make_embed(f'⏱️ 計時開始：{fmt_span(due_ts - now)}',
                               (f'### {message}\n' if message else '') + f'{when}響鈴', theme=THEME)
        else:
            embed = make_embed('⏰ 提醒已設定', f'### {message}\n{when}', theme=THEME)
        if not channel_id:
            # Only the user sees this, so there is nothing to clean up later.
            embed.description += '\n-# 你在這個頻道不能發言，時間到時會私訊你'
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed)
        # Remember this message so it can be removed once the reminder has fired (or been cancelled).
        confirmation = await interaction.original_response()
        await db.conn.execute('UPDATE reminders SET confirm_message_id = ? WHERE id = ?', (confirmation.id, cur.lastrowid))
        await db.conn.commit()

    async def _delete_confirmation(self, row) -> None:
        channel = self.bot.get_channel(row['channel_id'])
        if channel and row['confirm_message_id']:
            try:
                await channel.get_partial_message(row['confirm_message_id']).delete()
            except discord.HTTPException:
                pass  # already deleted, or no longer visible to the bot

    @remind.command(name='set')
    @app_commands.describe(when='例如 30m、1h30m、2d、14:00、10-25 14:00、2026-10-25 14:00（台灣時間）',
                           message='要提醒你什麼？')
    async def remind_set(self, interaction: discord.Interaction, when: str,
                         message: app_commands.Range[str, 1, 500]):
        """設定一個提醒"""
        due = parse_when(when)
        if due is None:
            await interaction.response.send_message(
                embed=error_embed('看不懂這個時間，可以試試 `30m`、`2h`、`14:00` 或 `2026-10-25 14:00`。'),
                ephemeral=True)
            return
        if due.timestamp() <= time.time():
            await interaction.response.send_message(embed=error_embed('這個時間已經過了。'), ephemeral=True)
            return
        await self._create(interaction, due.timestamp(), message, 'remind')

    @remind.command(name='list')
    async def remind_list(self, interaction: discord.Interaction):
        """查看你所有的提醒"""
        async with db.conn.execute('SELECT * FROM reminders WHERE user_id = ? ORDER BY due_ts',
                                   (interaction.user.id,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message(embed=note_embed('📭 你目前沒有任何提醒。', THEME), ephemeral=True)
            return
        text = '\n'.join(f'`#{r["id"]}`　**{truncate(describe(r), 80)}**\n-# {timestamp(r["due_ts"], "f")}（{timestamp(r["due_ts"], "R")}）'
                         for r in rows)
        embed = make_embed(f'⏰ 你的提醒（{len(rows)} 個）', text + '\n\n-# 用 `/remind cancel` 取消', theme=THEME)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def reminder_autocomplete(self, interaction: discord.Interaction, current: str):
        async with db.conn.execute('SELECT * FROM reminders WHERE user_id = ? ORDER BY due_ts LIMIT 25',
                                   (interaction.user.id,)) as cur:
            rows = await cur.fetchall()
        return [app_commands.Choice(name=truncate(f'#{r["id"]} {describe(r)}', 100), value=r['id'])
                for r in rows if current.lower() in f'#{r["id"]} {describe(r)}'.lower()]

    @remind.command(name='cancel')
    @app_commands.autocomplete(reminder=reminder_autocomplete)
    @app_commands.describe(reminder='要取消的提醒')
    async def remind_cancel(self, interaction: discord.Interaction, reminder: int):
        """取消一個提醒"""
        async with db.conn.execute('SELECT * FROM reminders WHERE id = ? AND user_id = ?',
                                   (reminder, interaction.user.id)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(embed=error_embed('找不到這個提醒。'), ephemeral=True)
            return
        await db.conn.execute('DELETE FROM reminders WHERE id = ?', (reminder,))
        await db.conn.commit()
        await self._delete_confirmation(row)
        await interaction.response.send_message(embed=note_embed(f'🗑️ 已取消：{truncate(describe(row), 80)}', THEME),
                                                ephemeral=True)

    @remind.command(name='timer')
    @app_commands.describe(duration='例如 90s、25m、1h30m（只寫數字代表分鐘）', label='選填：這次計時的名稱，例如「泡麵」')
    async def timer(self, interaction: discord.Interaction, duration: str,
                    label: app_commands.Range[str, 1, 100] | None = None):
        """開始倒數計時"""
        seconds = parse_duration(duration)
        if not seconds or seconds <= 0:
            await interaction.response.send_message(embed=error_embed('請輸入像 `90s`、`25m`、`1h30m` 這樣的時間。'),
                                                    ephemeral=True)
            return
        await self._create(interaction, time.time() + seconds, label or '', 'timer')

    @tasks.loop(seconds=5)
    @guarded
    async def check_due(self):
        async with db.conn.execute('SELECT * FROM reminders WHERE due_ts <= ?', (time.time(),)) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row['kind'] == 'timer':
                span = fmt_span(row['due_ts'] - row['created_ts'])
                title = '⏰ 時間到！'
                body = (f'### {row["message"]}\n' if row['message'] else '') + f'-# {span}計時結束'
            else:
                title = '⏰ 提醒時間到！'
                body = f'### {row["message"]}\n-# 設定於 {timestamp(row["created_ts"], "f")}'
            if time.time() - row['due_ts'] > 120:
                body += f'\n-# ⚠️ 晚送了：原定 {timestamp(row["due_ts"], "R")}，當時 bot 不在線上'
            embed = make_embed(title, body, theme=THEME)
            channel = self.bot.get_channel(row['channel_id'])
            try:
                if channel:
                    await channel.send(f'<@{row["user_id"]}>', embed=embed,
                                       allowed_mentions=discord.AllowedMentions(users=True))
                else:
                    user = await self.bot.fetch_user(row['user_id'])
                    await user.send(embed=embed)
            except discord.HTTPException:
                pass
            await self._delete_confirmation(row)  # the "set" message is no longer useful
            await db.conn.execute('DELETE FROM reminders WHERE id = ?', (row['id'],))
        if rows:
            await db.conn.commit()

    @check_due.before_loop
    async def before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminder(bot))
