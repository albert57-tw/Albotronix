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
from core import db, http
from core.cards import CardList, card
from core.embeds import CTF as THEME, error_embed, make_embed, note_embed, timestamp, truncate
from cogs.events import Event, upsert_events

log = logging.getLogger(__name__)

CTFTIME_API = 'https://ctftime.org/api/v1'
SOLVED = '✅ '
EVENTS_PER_PAGE = 4
TEAM_EVENTS = 8
# Team page: the per-year tabs and each result row (place, event, CTF points, rating points).
TEAM_TAB = re.compile(r'<div class="tab-pane[^"]*" id="rating_(\d{4})">(.*?)</table>', re.S)
TEAM_ROW = re.compile(r'<td class="place">(\d+)</td><td><a href="/event/(\d+)">([^<]+)</a></td>'
                      r'<td>[\d.]+</td><td>([\d.]+)</td>')


def _start_ts(event: dict) -> float:
    return datetime.datetime.fromisoformat(event['start']).timestamp()


def flag(country: str) -> str:
    """'TW' -> 🇹🇼"""
    return ''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in country.upper()) if len(country) == 2 else country


def fmt_length(duration: dict) -> str:
    days, hours = duration.get('days', 0), duration.get('hours', 0)
    return ' '.join(p for p in (f'{days} 天' if days else '', f'{hours} 小時' if hours else '') if p) or '—'


def event_text(event: dict) -> str:
    start = _start_ts(event)
    lines = [f'### [{truncate(event["title"], 100)}]({event["ctftime_url"]})',
             f'🕒 {timestamp(start, "f")}（{timestamp(start, "R")}）',
             f'⏳ {fmt_length(event["duration"])} · ⚖️ 權重 **{event["weight"]:.1f}** · 👥 {event["participants"]} 隊',
             f'🧩 {event["format"]}']
    if event.get('url'):
        lines.append(f'🔗 {event["url"]}')
    return '\n'.join(lines)


async def is_ctf_channel(channel) -> bool:
    """Whether /ctf start created this channel. Channels from before that was recorded are recognised by the
    name and topic /ctf start gives them, and recorded on first use."""
    if not isinstance(channel, discord.TextChannel):
        return False
    if await db.get_kv(f'ctf_channel:{channel.id}'):
        return True
    if channel.name == 'challenges' and '用 /ctf chal 為每題開討論串' in (channel.topic or ''):
        await db.set_kv(f'ctf_channel:{channel.id}', channel.category.name if channel.category else '')
        return True
    return False


def event_card(event: dict) -> discord.ui.Container:
    return card(event_text(event), event.get('logo'), THEME.color)


async def fetch_events(days: int) -> list[dict]:
    now = time.time()
    events = await http.get_json(f'{CTFTIME_API}/events/',
                                 params={'limit': 100, 'start': int(now), 'finish': int(now + days * 86400)})
    # Only online CTFs at or above the weight threshold; on-site and low-weight events are left out entirely.
    return sorted((e for e in events if not e.get('onsite') and e['weight'] >= config.CTF_MIN_WEIGHT),
                  key=_start_ts)


async def store_events(events: list[dict], days: int) -> None:
    await upsert_events([Event('ctftime', str(e['id']), e['title'], e['ctftime_url'], _start_ts(e))
                         for e in events])
    # Drop upcoming rows that are no longer listed (cancelled, on-site or below the weight threshold).
    now = time.time()
    ids = [str(e['id']) for e in events]
    await db.conn.execute(
        f'DELETE FROM events WHERE source = ? AND start_ts BETWEEN ? AND ? '
        f'AND event_id NOT IN ({",".join("?" * len(ids))})', ('ctftime', now, now + days * 86400, *ids))
    await db.conn.commit()


class CTF(commands.Cog):
    ctf = app_commands.Group(name='ctf', description='CTF 工具')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.daily_notify.start()
        self.refresh_loop.start()

    async def cog_unload(self) -> None:
        self.daily_notify.cancel()
        self.refresh_loop.cancel()

    @tasks.loop(hours=6)
    @guarded
    async def refresh_loop(self):
        """Keep two weeks of events in the DB for /contest upcoming and the 1-hour reminders."""
        try:
            await store_events(await fetch_events(14), 14)
        except Exception:
            log.warning('CTFtime refresh failed', exc_info=True)

    @tasks.loop(time=config.CTF_NOTIFY_TIME)
    @guarded
    async def daily_notify(self):
        channel = self.bot.get_channel(config.CTF_CHANNEL_ID or 0)
        if not channel:
            return
        events = await fetch_events(3)
        await store_events(events, 3)
        if not events:
            await channel.send(embed=note_embed('🚩 未來 3 天沒有 CTF 比賽。', THEME))
            return
        # One message per page (no buttons on scheduled posts).
        title = f'🚩 未來 3 天有 {len(events)} 場 CTF'
        cards = [event_card(e) for e in events]
        for page in range(len(CardList(cards, title, 'CTF', EVENTS_PER_PAGE).pages)):
            await channel.send(view=CardList(cards, title, 'CTF', EVENTS_PER_PAGE, page))

    @refresh_loop.before_loop
    @daily_notify.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

    @ctf.command(name='upcoming')
    @app_commands.describe(days='往後查幾天（1–14）')
    async def upcoming(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 14] = 7):
        """查詢 CTFtime 上即將舉行的 CTF"""
        await interaction.response.defer()
        events = await fetch_events(days)
        if not events:
            await interaction.followup.send(embed=note_embed(f'🚩 未來 {days} 天沒有 CTF 比賽。', THEME))
            return
        view = CardList([event_card(e) for e in events], f'🚩 未來 {days} 天的 CTF · 共 {len(events)} 場', 'CTF',
                        EVENTS_PER_PAGE, owner_id=interaction.user.id)
        view.message = await interaction.followup.send(view=view, wait=True)

    @ctf.command(name='team')
    @app_commands.describe(team_id='CTFtime 隊伍 ID（隊伍頁面網址裡的數字）')
    async def team(self, interaction: discord.Interaction, team_id: int):
        """查詢 CTFtime 隊伍排名"""
        await interaction.response.defer()
        try:
            team = await http.get_json(f'{CTFTIME_API}/teams/{team_id}/')
        except Exception:
            await interaction.followup.send(embed=error_embed('找不到這個隊伍。'))
            return
        country = team.get('country') or ''
        aliases = '、'.join(team.get('aliases') or [])
        description = ' · '.join(p for p in (f'{flag(country)} {country}' if country else '',
                                             '🎓 學術隊伍' if team.get('academic') else '',
                                             f'又名 {aliases}' if aliases else '') if p)
        embed = make_embed(f'🚩 {team["name"]}', description or None, theme=THEME,
                           url=f'https://ctftime.org/team/{team_id}', thumbnail=team.get('logo') or None)
        # Ranking per year, newest first; years with only a country place (no events played) are skipped.
        years = [(year, r) for year, r in sorted(team.get('rating', {}).items(), reverse=True) if r.get('rating_place')]
        lines = [f'`{year}`　🌍 第 **{r["rating_place"]}** 名'
                 + (f'　{flag(country) or "🏳️"} 第 **{r["country_place"]}** 名' if r.get('country_place') else '')
                 + f'　{r.get("rating_points", 0):.2f} 分' for year, r in years[:5]]
        embed.add_field(name='🏆 歷年排名', value='\n'.join(lines) or '還沒有排名紀錄', inline=False)
        # The API has no per-event results, so those come from the team page (newest year's tab).
        try:
            page = await http.get_text(f'https://ctftime.org/team/{team_id}')
            year, tab = next(TEAM_TAB.finditer(page)).groups()
            results = TEAM_ROW.findall(tab)
        except Exception:
            log.warning('CTFtime team page parse failed for %s', team_id, exc_info=True)
            results = []
        if results:
            rows = [f'`#{place:>4}` [{truncate(html.unescape(name), 40)}](https://ctftime.org/event/{event_id})'
                    f' · {float(points):.2f}' for place, event_id, name, points in results[:TEAM_EVENTS]]
            more = f'\n-# 共 {len(results)} 場，只列最近 {TEAM_EVENTS} 場' if len(results) > TEAM_EVENTS else ''
            embed.add_field(name=f'🚩 {year} 年參賽紀錄（名次 · 積分）', value='\n'.join(rows) + more, inline=False)
        await interaction.followup.send(embed=embed)

    @ctf.command(name='start')
    @app_commands.describe(name='CTF 名稱，例如「HITCON CTF 2026」')
    async def start(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90]):
        """為一場 CTF 建立分類與頻道（需要「管理頻道」權限）"""
        if not interaction.permissions.manage_channels:
            await interaction.response.send_message(embed=error_embed('你需要「管理頻道」權限。'), ephemeral=True)
            return
        await interaction.response.defer()
        guild = interaction.guild
        category = await guild.create_category(name, reason=f'/ctf start by {interaction.user}')
        channel = await category.create_text_channel(
            'challenges', topic=f'{name} — 用 /ctf chal 為每題開討論串，解出後在串裡用 /ctf solved')
        await db.set_kv(f'ctf_channel:{channel.id}', name)
        await interaction.followup.send(embed=note_embed(f'🚩 已建立 {channel.mention}，祝順利拿到 flag！', THEME))
        embed = make_embed(f'🚩 {name}', theme=THEME)
        embed.add_field(name='1️⃣ 開題目', value='在這個頻道用 `/ctf chal` 為每一題開一個討論串', inline=False)
        embed.add_field(name='2️⃣ 解出來了', value='在題目的討論串裡用 `/ctf solved`（可以附上 flag）', inline=False)
        await channel.send(embed=embed)

    @ctf.command(name='chal')
    @app_commands.describe(name='題目名稱', category='題目類別，例如 web、pwn、crypto、rev、misc')
    async def chal(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 60],
                   category: app_commands.Range[str, 1, 20]):
        """在 /ctf start 建立的 CTF 頻道裡，為一道題目開討論串"""
        if not await is_ctf_channel(interaction.channel):
            await interaction.response.send_message(
                embed=error_embed('只能在 /ctf start 建立的 CTF 頻道裡使用（請管理員先用 /ctf start 開一場 CTF）。'),
                ephemeral=True)
            return
        # The bot creates the thread, so check that the user could have done it themselves.
        if not interaction.permissions.create_public_threads:
            await interaction.response.send_message(embed=error_embed('你在這個頻道沒有「建立公開討論串」權限。'),
                                                    ephemeral=True)
            return
        thread = await interaction.channel.create_thread(name=f'[{category.lower()}] {name}',
                                                         type=discord.ChannelType.public_thread,
                                                         auto_archive_duration=10080)
        await thread.send(embed=note_embed(f'🧩 **{name}**（{category}）由 {interaction.user.mention} 開啟，一起來解吧！',
                                           THEME))
        await interaction.response.send_message(embed=note_embed(f'🧩 題目討論串：{thread.mention}', THEME))

    @ctf.command(name='solved')
    @app_commands.describe(flag='選填：flag（會用防雷標記藏起來）')
    async def solved(self, interaction: discord.Interaction, flag: str | None = None):
        """把目前的題目討論串標記為已解出"""
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(embed=error_embed('請在題目的討論串裡使用。'), ephemeral=True)
            return
        # Only challenge threads (opened by /ctf chal, so owned by the bot) — not anyone's thread.
        if thread.owner_id != interaction.client.user.id and not interaction.permissions.manage_threads:
            await interaction.response.send_message(embed=error_embed('只能在用 /ctf chal 開的題目討論串裡使用。'),
                                                    ephemeral=True)
            return
        if not thread.name.startswith(SOLVED):
            await thread.edit(name=truncate(SOLVED + thread.name, 100))
        text = f'🎉 **{interaction.user.mention} 解出來了！**'
        if flag:
            text += f'\nFlag：||`{flag}`||'
        await interaction.response.send_message(embed=note_embed(text, THEME))


async def setup(bot: commands.Bot):
    await bot.add_cog(CTF(bot))
