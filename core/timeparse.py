"""Parse the human-friendly times and dates users type into slash commands."""
import datetime
import re

import discord
from discord import app_commands

import config

_UNITS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
_RELATIVE = re.compile(r'(\d+(?:\.\d+)?)\s*([dhms])', re.I)


def now() -> datetime.datetime:
    return datetime.datetime.now(config.TZ)


def parse_duration(text: str) -> float | None:
    """'90' / '30s' / '1h30m' / '2d' -> seconds. Returns None if it isn't a duration."""
    text = text.strip().lower().replace(' ', '')
    if re.fullmatch(r'\d+(\.\d+)?', text):
        return float(text) * 60  # a bare number means minutes
    if not text or _RELATIVE.sub('', text):
        return None
    return sum(float(n) * _UNITS[u.lower()] for n, u in _RELATIVE.findall(text))


def parse_when(text: str) -> datetime.datetime | None:
    """A duration ('30m'), a clock time ('14:00', today or tomorrow) or a full date-time
    ('2026-10-25 14:00', '10-25 14:00'). All absolute times are Taiwan time (UTC+8)."""
    text = text.strip()
    current = now()
    seconds = parse_duration(text)
    if seconds is not None:
        return current + datetime.timedelta(seconds=seconds)
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M'):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=config.TZ)
        except ValueError:
            pass
    for fmt in ('%m-%d %H:%M', '%m/%d %H:%M'):
        try:
            parsed = datetime.datetime.strptime(text, fmt).replace(year=current.year, tzinfo=config.TZ)
            return parsed if parsed > current else parsed.replace(year=current.year + 1)
        except ValueError:
            pass
    try:
        clock = datetime.datetime.strptime(text, '%H:%M').time()
    except ValueError:
        return None
    parsed = datetime.datetime.combine(current.date(), clock, tzinfo=config.TZ)
    return parsed if parsed > current else parsed + datetime.timedelta(days=1)


def parse_date(text: str) -> datetime.date | None:
    """'today' / 'tomorrow' / '+3' / '2026-10-25' / '10-25'."""
    text = text.strip().lower()
    today = now().date()
    if text in ('', 'today', '今天'):
        return today
    if text in ('tomorrow', '明天'):
        return today + datetime.timedelta(days=1)
    if re.fullmatch(r'\+\d+', text):
        return today + datetime.timedelta(days=int(text[1:]))
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    for fmt in ('%m-%d', '%m/%d'):
        try:
            parsed = datetime.datetime.strptime(text, fmt).date().replace(year=today.year)
            return parsed if parsed >= today else parsed.replace(year=today.year + 1)
        except ValueError:
            pass
    return None


async def date_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Suggest the next two weeks; whatever the user typed is still accepted."""
    today = now().date()
    choices = []
    for offset in range(14):
        day = today + datetime.timedelta(days=offset)
        weekday = '週' + '一二三四五六日'[day.weekday()]
        label = {0: f'今天 {weekday}', 1: f'明天 {weekday}'}.get(offset, weekday)
        value = day.isoformat()
        if current.lower() in value or current in label:
            choices.append(app_commands.Choice(name=f'{value}（{label}）', value=value))
    return choices[:25]
