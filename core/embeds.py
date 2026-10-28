"""The bot's visual language: one colour + footer label per feature, compact and in Traditional Chinese."""
import datetime
from dataclasses import dataclass

import discord

import config
from core import icons


@dataclass(frozen=True)
class Theme:
    label: str
    color: int


# Every feature uses the original Albotronix teal; only status colours (errors, alerts, severity) differ.
BRAND = config.EMBED_COLOR
GENERAL = Theme('Albotronix', BRAND)
THSR = Theme('高鐵', BRAND)
TRA = Theme('台鐵', BRAND)
METRO = Theme('捷運', BRAND)
BUS = Theme('公車', BRAND)
WEATHER = Theme('天氣', BRAND)
QUAKE = Theme('地震', BRAND)
CTF = Theme('CTF', BRAND)
CONTEST = Theme('競程', BRAND)
SECURITY = Theme('資安', BRAND)
FINANCE = Theme('匯率油價', BRAND)
REMINDER = Theme('提醒', BRAND)
POLL = Theme('投票', BRAND)
AI = Theme('AI', BRAND)

SUCCESS = 0x57F287
DANGER = 0xED4245
MUTED = 0x80848E

WEEKDAYS = '一二三四五六日'


def make_embed(title: str | None = None, description: str | None = None, *, theme: Theme = GENERAL,
               color: int | None = None, url: str | None = None, thumbnail: str | None = None,
               stamp: bool = False) -> discord.Embed:
    """`stamp` adds the current time to the footer — use it for live data (seats, weather, rates)."""
    embed = discord.Embed(title=title, description=description, color=theme.color if color is None else color,
                          url=url)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    label = '' if theme is GENERAL else f'{theme.label} · '
    embed.set_footer(text=f'{label}{config.BOT_NAME} © {config.FOOTER_TEXT}', icon_url=icons.url('brand_avatar'))
    if stamp:
        embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed


def note_embed(message: str, theme: Theme = GENERAL) -> discord.Embed:
    """A one-line confirmation ("⏸ 已暫停") in the feature's colour, without footer clutter."""
    return discord.Embed(description=message, color=theme.color)


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(description=f'❌ {message}', color=DANGER)




def timestamp(ts: float, style: str = 'F') -> str:
    """Discord timestamp markup — every viewer sees it in their own timezone."""
    return f'<t:{int(ts)}:{style}>'


def fmt_date(day: datetime.date) -> str:
    """2026-10-25 -> '10/25（六）'"""
    return f'{day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）'


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def fit_rows(rows: list[str], more: str = '還有 {} 班', limit: int = 1024) -> str:
    """Join rows for one embed field, dropping the ones that don't fit and saying how many were left out."""
    kept, used = [], 0
    for i, row in enumerate(rows):
        tail = f'\n-# {more.format(len(rows) - i - 1)}' if i < len(rows) - 1 else ''
        if used + len(row) + 1 + len(tail) > limit and kept:
            kept.append(f'-# {more.format(len(rows) - i)}')
            break
        kept.append(row)
        used += len(row) + 1
    return '\n'.join(kept)


def chunk_text(text: str, limit: int = 2000) -> list[str]:
    """Split text into Discord-sized messages, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')
    if text:
        chunks.append(text)
    return chunks




def paginate_groups(groups: list[tuple[str, list[str]]], max_lines: int, make_page) -> list[discord.Embed]:
    """Pages of headed groups (e.g. one date and its events) that never split a group across pages,
    unless a single group is longer than a whole page — then it continues under a '（續）' heading."""
    pages, body, used = [], [], 0
    for heading, items in groups:
        while items:
            room = max_lines - used - 1  # one line for the heading
            if body and room < min(len(items), max_lines - 1):
                pages.append(make_page('\n\n'.join(body)))  # this group fits better on a fresh page
                body, used = [], 0
                continue
            chunk, items = items[:room], items[room:]
            body.append('\n'.join([heading] + chunk))
            used += len(chunk) + 1
            heading = f'{heading}（續）'
    if body or not pages:
        pages.append(make_page('\n\n'.join(body)))
    return pages


def paginate_fields(fields: list[tuple[str, str]], per_page: int, make_page, inline: bool = False) -> list[discord.Embed]:
    """Pages of fields, one block per item (inline=True makes a 3-column grid on desktop).
    make_page() returns a fresh embed that the fields are added to."""
    pages = []
    for i in range(0, len(fields), per_page):
        page = make_page()
        for name, value in fields[i:i + per_page]:
            page.add_field(name=name, value=value, inline=inline)
        pages.append(page)
    return pages or [make_page()]
