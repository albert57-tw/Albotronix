"""Shared "starts in 1 hour" reminders for CTFs and programming contests.

ctf.py and contests.py write rows into the `events` table with upsert_events(); this cog
sends one reminder per event and cleans up old rows.
"""
import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import db, icons
from core.embeds import CONTEST, CTF, make_embed, timestamp
from cogs.subscriptions import ping_kwargs

log = logging.getLogger(__name__)

# source: (application emoji, fallback, name)
SOURCES = {'ctftime': (None, '🚩', 'CTF'), 'codeforces': ('contest_cf', '🟦', 'Codeforces'),
           'atcoder': ('contest_ac', '⬛', 'AtCoder')}


def source_label(source: str) -> str:
    emoji, fallback, name = SOURCES.get(source, (None, '', source))
    return f'{icons.get(emoji, fallback) if emoji else fallback} {name}'.strip()


@dataclass
class Event:
    source: str
    event_id: str
    title: str
    url: str
    start_ts: float
    remind: bool = True


async def upsert_events(events: list[Event]) -> None:
    await db.conn.executemany(
        'INSERT INTO events (source, event_id, title, url, start_ts, remind) VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT (source, event_id) DO UPDATE SET title = excluded.title, url = excluded.url, '
        'remind = excluded.remind, '
        # If the start time moved, allow a fresh reminder.
        'notified = CASE WHEN events.start_ts = excluded.start_ts THEN events.notified ELSE 0 END, '
        'start_ts = excluded.start_ts',
        [(e.source, e.event_id, e.title, e.url, e.start_ts, int(e.remind)) for e in events])
    await db.conn.commit()


async def upcoming(days: int) -> list[Event]:
    now = time.time()
    async with db.conn.execute('SELECT * FROM events WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts',
                               (now, now + days * 86400)) as cur:
        return [Event(r['source'], r['event_id'], r['title'], r['url'], r['start_ts'], bool(r['remind']))
                for r in await cur.fetchall()]


def channel_for(source: str) -> int | None:
    return config.CTF_CHANNEL_ID if source == 'ctftime' else config.CONTEST_CHANNEL_ID


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.remind_loop.start()

    async def cog_unload(self) -> None:
        self.remind_loop.cancel()

    @tasks.loop(seconds=30)
    @guarded
    async def remind_loop(self):
        now = time.time()
        async with db.conn.execute(
                'SELECT * FROM events WHERE notified = 0 AND remind = 1 AND start_ts > ? AND start_ts <= ?',
                (now, now + config.EVENT_REMIND_MINUTES * 60)) as cur:
            rows = await cur.fetchall()
        for row in rows:
            channel = self.bot.get_channel(channel_for(row['source']) or 0)
            if channel:
                label = source_label(row['source'])
                embed = make_embed('⏰ 比賽即將開始', f'### [{row["title"]}]({row["url"]})\n'
                                   f'{label} · {timestamp(row["start_ts"], "t")} 開始（{timestamp(row["start_ts"], "R")}）',
                                   theme=CTF if row['source'] == 'ctftime' else CONTEST)
                topic = 'ctf' if row['source'] == 'ctftime' else 'contest'
                try:
                    await channel.send(embed=embed, **await ping_kwargs(self.bot, topic))
                except discord.HTTPException:
                    log.warning('Could not send reminder for %s', row['title'], exc_info=True)
            await db.conn.execute('UPDATE events SET notified = 1 WHERE source = ? AND event_id = ?',
                                  (row['source'], row['event_id']))
        await db.conn.execute('DELETE FROM events WHERE start_ts < ?', (now - 30 * 86400,))
        await db.conn.commit()

    @remind_loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
