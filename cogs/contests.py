import datetime
import html
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import cses, http, icons
from core.embeds import CONTEST as THEME, error_embed, fmt_date, make_embed, note_embed, paginate_groups, truncate
from core.paginator import send_pages
from cogs.events import Event, source_label, upcoming, upsert_events

log = logging.getLogger(__name__)

ATCODER_ROW = re.compile(r"<time class='fixtime fixtime-full'>([^<]+)</time>.*?<a href=\"/contests/([^\"]+)\">([^<]+)</a>",
                         re.S)
LEETCODE_QUERY = '''query { activeDailyCodingChallengeQuestion { date link
  question { title difficulty acRate questionFrontendId topicTags { name } } } }'''
DIFFICULTY = {'Easy': ('🟢 簡單', 0x00B8A3), 'Medium': ('🟡 中等', 0xFFC01E), 'Hard': ('🔴 困難', 0xFF375F)}


async def fetch_codeforces() -> list[Event]:
    data = await http.get_json('https://codeforces.com/api/contest.list', params={'gym': 'false'})
    horizon = time.time() + 14 * 86400
    return [Event('codeforces', str(c['id']), c['name'], f'https://codeforces.com/contests/{c["id"]}',
                  c['startTimeSeconds'])
            for c in data['result'] if c['phase'] == 'BEFORE' and c.get('startTimeSeconds', 0) < horizon]


async def fetch_atcoder() -> list[Event]:
    """AtCoder has no API, so this reads the 'upcoming' table on the contests page."""
    page = await http.get_text('https://atcoder.jp/contests/?lang=en')
    start = page.find('id="contest-table-upcoming"')
    if start == -1:
        raise ValueError('AtCoder page layout changed: upcoming table not found')
    table = page[start:page.find('</table>', start)]
    events = []
    for when, slug, title in ATCODER_ROW.findall(table):
        start_ts = datetime.datetime.strptime(when, '%Y-%m-%d %H:%M:%S%z').timestamp()
        events.append(Event('atcoder', slug, html.unescape(title).strip(), f'https://atcoder.jp/contests/{slug}', start_ts))
    return events


async def fetch_leetcode_daily() -> dict:
    data = await http.post_json('https://leetcode.com/graphql', json={'query': LEETCODE_QUERY},
                                headers={'Referer': 'https://leetcode.com'})
    return data['data']['activeDailyCodingChallengeQuestion']


def leetcode_embed(daily: dict) -> discord.Embed:
    q = daily['question']
    label, color = DIFFICULTY.get(q['difficulty'], (q['difficulty'], THEME.color))
    embed = make_embed(f'{q["questionFrontendId"]}. {q["title"]}', url=f'https://leetcode.com{daily["link"]}',
                       description=f'📅 **LeetCode 每日一題** · {daily["date"]}', theme=THEME, color=color,
                       thumbnail=icons.url('brand_leetcode'))
    embed.add_field(name='難度', value=label)
    embed.add_field(name='通過率', value=f'{q["acRate"]:.1f}%')
    tags = '、'.join(t['name'] for t in q['topicTags'])
    if tags:
        embed.add_field(name='主題（防雷）', value=f'||{tags}||')
    return embed


def cses_embed(task: cses.Task, day: int, limits: dict[str, str], notes: dict | None, text: str) -> discord.Embed:
    """Today's CSES problem as a short lesson: what it asks, the ideas behind it, hints hidden as spoilers."""
    zh, teaches, chapters = cses.CATEGORIES.get(task.category, (task.category, None, None))
    meta = [f'{zh}（{task.category}）第 {task.position}/{task.category_size} 題']
    if task.success_rate is not None:
        meta.append(f'通過率 {task.success_rate:.0f}%')
    meta += [f'⏱️ {limits["Time"]}' if 'Time' in limits else '', f'💾 {limits["Memory"]}' if 'Memory' in limits else '']
    embed = make_embed(f'📘 {task.name}', f'**CSES 每日一題** · 第 {day} 天\n-# {" · ".join(m for m in meta if m)}',
                       url=task.url, theme=THEME)
    if notes:
        embed.add_field(name='▸ 題目大意', value=truncate(notes['summary'], 1024), inline=False)
        concepts = '、'.join(notes.get('concepts') or [])
        why = notes.get('why') or ''
        if concepts or why:
            embed.add_field(name='▸ 會用到的觀念', value=truncate(f'**{concepts}**\n{why}'.strip(), 1024), inline=False)
        for i, hint in enumerate((notes.get('hints') or [])[:3], 1):
            embed.add_field(name=f'💡 提示 {i}（點開看）', value=truncate(f'||{hint}||', 1024), inline=False)
        if notes.get('complexity'):
            embed.add_field(name='⏱️ 目標複雜度（點開看）', value=truncate(f'||{notes["complexity"]}||', 1024), inline=False)
        if pitfalls := notes.get('pitfalls'):
            embed.add_field(name='⚠️ 常見陷阱', value=truncate('\n'.join(f'• {p}' for p in pitfalls), 1024), inline=False)
    else:
        # No AI notes: the statement's opening and what this category is about.
        if text:
            embed.add_field(name='▸ 題目（英文）', value=truncate(text.split('\n\n')[0], 600), inline=False)
        if teaches:
            embed.add_field(name='▸ 這個分類在練什麼', value=teaches, inline=False)
    if chapters:
        embed.add_field(name='📖 延伸閱讀', value=f"[Competitive Programmer's Handbook]({cses.BOOK_URL}) {chapters}",
                        inline=False)
    return embed


async def cses_today() -> discord.Embed:
    task, day = await cses.today_task()
    text, limits = await cses.statement(task)
    return cses_embed(task, day, limits, await cses.lesson(task, text), text)


class Contests(commands.Cog):
    contest = app_commands.Group(name='contest', description='競程比賽、LeetCode 與 CSES 每日一題')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.refresh_loop.start()
        self.leetcode_loop.start()
        self.cses_loop.start()

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()
        self.leetcode_loop.cancel()
        self.cses_loop.cancel()

    async def refresh(self) -> None:
        for name, fetch in (('Codeforces', fetch_codeforces), ('AtCoder', fetch_atcoder)):
            try:
                await upsert_events(await fetch())
            except Exception:
                log.warning('%s refresh failed', name, exc_info=True)

    @tasks.loop(time=config.CONTEST_REFRESH_TIME)
    @guarded
    async def refresh_loop(self):
        await self.refresh()

    @refresh_loop.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()
        await self.refresh()  # also fill the table right after startup

    @tasks.loop(time=config.LEETCODE_TIME)
    @guarded
    async def leetcode_loop(self):
        channel = self.bot.get_channel(config.CONTEST_CHANNEL_ID or 0)
        if channel:
            await channel.send(embed=leetcode_embed(await fetch_leetcode_daily()))

    @tasks.loop(time=config.CSES_TIME)
    @guarded
    async def cses_loop(self):
        channel = self.bot.get_channel(config.CSES_CHANNEL_ID or 0)
        if channel:
            await channel.send(embed=await cses_today())

    @cses_loop.before_loop
    @leetcode_loop.before_loop
    async def before_leetcode(self):
        await self.bot.wait_until_ready()

    @contest.command(name='upcoming')
    @app_commands.describe(days='往後查幾天（1–14）')
    async def upcoming_cmd(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 14] = 7):
        """近期的 CTF、Codeforces、AtCoder 比賽"""
        events = await upcoming(days)
        if not events:
            await interaction.response.send_message(embed=note_embed(f'🗓️ 未來 {days} 天沒有比賽。', THEME))
            return
        # Group by day in Taiwan time; a day is never split across pages.
        groups: dict[datetime.date, list[str]] = {}
        for e in events:
            start = datetime.datetime.fromtimestamp(e.start_ts, config.TZ)
            groups.setdefault(start.date(), []).append(
                f'`{start:%H:%M}` {source_label(e.source)}　[{truncate(e.title, 60)}]({e.url})')
        pages = paginate_groups([(f'**{fmt_date(day)}**', lines) for day, lines in groups.items()], 16, lambda body: make_embed(
            f'🗓️ 未來 {days} 天的比賽（{len(events)} 場）', body + '\n\n-# 時間為台灣時間 · 開賽前 1 小時會提醒',
            theme=THEME))
        await send_pages(interaction, pages)

    @contest.command(name='cses')
    async def cses_daily(self, interaction: discord.Interaction):
        """今天的 CSES 每日一題，附觀念說明與解題提示"""
        await interaction.response.defer()
        try:
            await interaction.followup.send(embed=await cses_today())
        except Exception:
            log.warning('CSES fetch failed', exc_info=True)
            await interaction.followup.send(embed=error_embed('目前連不到 CSES，請稍後再試。'))

    @contest.command(name='leetcode')
    async def leetcode_daily(self, interaction: discord.Interaction):
        """今天的 LeetCode 每日一題"""
        await interaction.response.defer()
        try:
            await interaction.followup.send(embed=leetcode_embed(await fetch_leetcode_daily()))
        except Exception:
            log.warning('LeetCode fetch failed', exc_info=True)
            await interaction.followup.send(embed=error_embed('目前連不到 LeetCode，請稍後再試。'))


async def setup(bot: commands.Bot):
    await bot.add_cog(Contests(bot))
