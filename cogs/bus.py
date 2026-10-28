"""/bus stop: every route's buses due at a stop within the next 30 minutes (TDX city bus data, all of Taiwan)."""
import asyncio
import datetime
import json
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from core import FeatureDisabled, db, tdx
from core.embeds import BUS as THEME, error_embed, fit_rows, make_embed

log = logging.getLogger(__name__)

BOARD_MINUTES = 30
STOPS_CACHE_DAYS = 7  # stop lists rarely change; kept in the DB
# TDX city code for each city / county, in the usual north-to-south order.
CITIES = {
    '基隆市': 'Keelung', '臺北市': 'Taipei', '新北市': 'NewTaipei', '桃園市': 'Taoyuan', '新竹市': 'Hsinchu',
    '新竹縣': 'HsinchuCounty', '苗栗縣': 'MiaoliCounty', '臺中市': 'Taichung', '彰化縣': 'ChanghuaCounty',
    '南投縣': 'NantouCounty', '雲林縣': 'YunlinCounty', '嘉義市': 'Chiayi', '嘉義縣': 'ChiayiCounty',
    '臺南市': 'Tainan', '高雄市': 'Kaohsiung', '屏東縣': 'PingtungCounty', '宜蘭縣': 'YilanCounty',
    '花蓮縣': 'HualienCounty', '臺東縣': 'TaitungCounty', '澎湖縣': 'PenghuCounty', '金門縣': 'KinmenCounty',
    '連江縣': 'LienchiangCounty',
}
CITY_NAMES = {code: name for name, code in CITIES.items()}
STOP_STATUS = {1: '尚未發車', 2: '交管不停靠', 3: '末班車已過', 4: '今日未營運'}
PLATFORM = re.compile(r'^(.*?)\s*\(([^()]*)\)$')  # '臺南火車站(中山路C)' -> ('臺南火車站', '中山路C')


def default_city() -> str:
    """The bus city defaults to the weather city (WEATHER_CITY), else Taipei."""
    return CITIES.get(config.WEATHER_CITY, 'Taipei')


def split_platform(name: str) -> tuple[str, str]:
    match = PLATFORM.match(name)
    return (match.group(1), match.group(2)) if match else (name, '')


def destination(row: dict, ends: dict[str, tuple[str, str]]) -> str:
    """Where this bus is heading: the sub-route's own end ('18路 和順轉運站→大成國中 [區間]' -> '往大成國中',
    which also covers short runs), else the route's terminus for its direction, else just the direction."""
    sub_route = (row.get('SubRouteName') or {}).get('Zh_tw', '')
    if '→' in sub_route:
        return '往' + re.sub(r'\s*\[.*?\]\s*$', '', sub_route.rsplit('→', 1)[1]).strip()
    departure, terminus = ends.get(row.get('RouteUID', ''), ('', ''))
    end = terminus if row.get('Direction') == 0 else departure if row.get('Direction') == 1 else ''
    return f'往{end}' if end else {0: '去程', 1: '返程'}.get(row.get('Direction'), '')


class Bus(commands.Cog):
    bus = app_commands.Group(name='bus', description='公車：站牌即時到站')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.stops: dict[str, dict[str, list[str]]] = {}  # city -> base name -> each platform's full stop name
        self._loading: asyncio.Task | None = None

    async def cog_load(self) -> None:
        if not (config.TDX_ID and config.TDX_SECRET):
            raise FeatureDisabled('TDX_ID / TDX_SECRET not set')
        self._loading = asyncio.create_task(self._load_all())

    async def cog_unload(self) -> None:
        if self._loading:
            self._loading.cancel()

    async def _load_all(self) -> None:
        """Every city's stop list, the bot's own city first. TDX allows 5 calls a minute, so a cold start
        takes a few minutes; /bus searches whatever has loaded so far meanwhile."""
        order = [default_city()] + [c for c in CITIES.values() if c != default_city()]
        for city in order:
            delay = 15
            while True:
                try:
                    if await self._load_city(city):
                        await asyncio.sleep(13)  # fetched from TDX: stay under the rate limit
                    break
                except Exception:
                    log.info('Bus stops for %s not loaded yet, retrying in %ss', city, delay, exc_info=True)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 600)

    async def _load_city(self, city: str) -> bool:
        """Load one city's stops from the DB, or from TDX when missing or older than a week (returns True)."""
        cached = await db.get_kv(f'bus_stops:{city}')
        if cached:
            data = json.loads(cached)
            self.stops[city] = data['stops']
            if time.time() - data['fetched'] < STOPS_CACHE_DAYS * 86400:
                return False
        rows = await tdx.get(f'/v2/Bus/Stop/City/{city}', **{'$select': 'StopName'})
        grouped: dict[str, list[str]] = {}
        for name in sorted({r['StopName']['Zh_tw'] for r in rows if r.get('StopName')}):
            grouped.setdefault(split_platform(name)[0], []).append(name)
        self.stops[city] = grouped
        await db.set_kv(f'bus_stops:{city}', json.dumps({'fetched': time.time(), 'stops': grouped}, ensure_ascii=False))
        return True

    async def stop_autocomplete(self, interaction: discord.Interaction, current: str):
        chosen = getattr(interaction.namespace, 'city', None)
        query = current.strip().replace('台', '臺')
        home = default_city()
        # Names starting with what was typed first, then shorter names, the bot's own city first on ties.
        matches = sorted(((name, city) for city, grouped in self.stops.items() if not chosen or city == chosen
                          for name in grouped if query in name),
                         key=lambda m: (not m[0].startswith(query), len(m[0]), m[1] != home, m[0]))
        return [app_commands.Choice(name=f'{name}（{CITY_NAMES[city]}）', value=f'{city}:{name}')
                for name, city in matches[:25]]

    async def route_ends(self, city: str) -> dict[str, tuple[str, str]]:
        """RouteUID -> (departure stop, destination stop), cached for a day; empty if TDX is unavailable."""
        try:
            rows = await tdx.get(f'/v2/Bus/Route/City/{city}', ttl=86400,
                                 **{'$select': 'RouteUID,DepartureStopNameZh,DestinationStopNameZh'})
        except tdx.TDXError:
            return {}
        return {r['RouteUID']: (r.get('DepartureStopNameZh') or '', r.get('DestinationStopNameZh') or '') for r in rows}

    def find(self, stop: str, city: str | None) -> tuple[str, list[str]] | None:
        """(city, stop names) for an autocomplete value ('Tainan:臺南火車站') or a typed name."""
        if ':' in stop and stop.split(':', 1)[0] in CITY_NAMES:
            city, stop = stop.split(':', 1)
        stop = stop.strip().replace('台', '臺')
        cities = [city] if city else [default_city()] + [c for c in self.stops if c != default_city()]
        for c in cities:
            grouped = self.stops.get(c, {})
            names = grouped.get(stop) or grouped.get(split_platform(stop)[0])
            if names:
                return c, names
        return None

    @bus.command(name='stop')
    @app_commands.describe(stop='站牌名稱，全台都可以搜尋（同一站的各個站牌會一起查）', city='選填：只搜尋這個縣市')
    @app_commands.choices(city=[app_commands.Choice(name=name, value=code) for name, code in CITIES.items()])
    @app_commands.autocomplete(stop=stop_autocomplete)
    async def stop(self, interaction: discord.Interaction, stop: str, city: str | None = None):
        """站牌即時到站：每條路線 30 分鐘內的公車"""
        await interaction.response.defer()
        if city and city not in CITY_NAMES:
            city = None  # not one of the choices (a hand-crafted request): search everywhere
        if city and city not in self.stops:
            await self._load_city(city)  # not loaded yet in the background: fetch it now
        found = self.find(stop, city)
        if not found:
            hint = '' if len(self.stops) == len(CITIES) else '（站牌資料還在載入中，可以稍後再試）'
            await interaction.followup.send(embed=error_embed(f'找不到「{stop.split(":")[-1]}」這個站牌，'
                                                              f'請從選單挑選站名。{hint}'))
            return
        city, names = found
        base = split_platform(names[0])[0]
        stop_filter = ' or '.join(f"StopName/Zh_tw eq '{n.replace(chr(39), chr(39) * 2)}'" for n in names)
        rows = await tdx.get(f'/v2/Bus/EstimatedTimeOfArrival/City/{city}', ttl=30, **{'$filter': stop_filter})
        ends = await self.route_ends(city)

        now = datetime.datetime.now(config.TZ)
        platforms: dict[str, list[tuple[int, str]]] = {}
        later, seen = set(), set()
        for r in rows:
            route = (r.get('RouteName') or {}).get('Zh_tw', '')
            platform = split_platform((r.get('StopName') or {}).get('Zh_tw', ''))[1]
            key = (platform, route, r.get('Direction'))
            if not route or key in seen:
                continue  # the same route and direction can be listed twice (sub-routes)
            eta = r.get('EstimateTime')
            if eta is None or eta > BOARD_MINUTES * 60 or r.get('StopStatus') in STOP_STATUS:
                later.add(route)
                continue
            seen.add(key)
            clock = f'{now + datetime.timedelta(seconds=eta):%H:%M}'
            when = '**即將進站**' if eta < 60 else f'{eta // 60} 分鐘後'
            platforms.setdefault(platform, []).append((eta, f'`{clock}`　**{route}**　{destination(r, ends)}　{when}'))
        later -= {route for (_, route, _) in seen}

        embed = make_embed(f'🚌 {base} 即時到站', f'-# {CITY_NAMES[city]} · {BOARD_MINUTES} 分鐘內的公車 · 時刻為預估到站時間',
                           theme=THEME, stamp=True)
        for platform in sorted(platforms):
            trips = [text for _, text in sorted(platforms[platform])]
            embed.add_field(name=f'▸ {platform or base}', value=fit_rows(trips), inline=False)
        if later:
            routes = '、'.join(sorted(later, key=lambda r: (len(r), r)))
            embed.add_field(name='​', value=f'-# {BOARD_MINUTES} 分鐘內沒有車：{routes}'[:1024], inline=False)
        if not platforms and not later:
            await interaction.followup.send(embed=error_embed('目前沒有這個站牌的到站資料。'))
            return
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bus(bot))
