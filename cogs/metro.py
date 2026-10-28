"""/metro: Taiwan metro systems via TDX — live arrivals, fares + travel time, first/last trains, disruptions."""
import asyncio
import datetime
import heapq
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core import FeatureDisabled, db, icons, tdx
from core.embeds import DANGER, METRO as THEME, SUCCESS, error_embed, make_embed, truncate
from core.loops import guarded

log = logging.getLogger(__name__)

SYSTEMS = {'TRTC': '台北捷運', 'NTMC': '新北捷運', 'KRTC': '高雄捷運', 'TYMC': '桃園機場捷運', 'TMRT': '台中捷運'}
LIVE_SYSTEMS = ('TRTC', 'KRTC', 'TYMC')            # TDX has live arrival boards only for these
ALERT_SYSTEMS = ('TRTC', 'KRTC', 'TYMC', 'TMRT')   # ... and disruption feeds only for these
LINE_NAMES = {
    ('TRTC', 'BL'): '板南線', ('TRTC', 'R'): '淡水信義線', ('TRTC', 'G'): '松山新店線', ('TRTC', 'O'): '中和新蘆線',
    ('TRTC', 'BR'): '文湖線', ('TRTC', 'Y'): '環狀線', ('NTMC', 'Y'): '環狀線', ('NTMC', 'LB'): '三鶯線',
    ('KRTC', 'O'): '橘線', ('KRTC', 'R'): '紅線', ('TYMC', 'A'): '機場線', ('TMRT', 'G'): '綠線',
}
SERVICE_STATUS = {1: '尚未發車', 2: '交管不停靠', 3: '末班車已過', 4: '今日未營運'}
TRANSFER_SECONDS = 300  # walking + waiting when changing lines
CROSS_TRANSFER_SECONDS = 600  # changing between operators (e.g. Taipei MRT <-> Airport MRT) is a longer walk
NORTH_SYSTEMS = ('TRTC', 'NTMC', 'TYMC')  # connected to each other; the others stand alone
STATION_CACHE_DAYS = 7
BOARD_MINUTES = 60
# Airport MRT train types (TDX TrainType): badge emoji and text fallback
TRAIN_TYPES = {1: ('tymc_commuter', '普通車'), 2: ('tymc_express', '直達車')}
WEEKDAY_KEYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def _norm(name: str) -> str:
    """Compare station names loosely: 臺/台, and the Airport MRT's '三重站' vs Taipei MRT's '三重'."""
    return name.replace('臺', '台').strip().lower().removesuffix('站')


def station_title(name: str) -> str:
    """'台北車站' already ends in 站; '淡水' becomes '淡水站'."""
    return name if name.endswith('站') else f'{name}站'


def line_name(system: str, line_id: str) -> str:
    return LINE_NAMES.get((system, line_id), f'{line_id} 線')


def line_badge(system: str, line_id: str) -> str:
    """The line's coloured round badge (application emoji), or '' if there isn't one.
    New Taipei runs the Circular line (Y) too, so it shares Taipei's badge."""
    key = f'{system.lower()}_{line_id}'
    return icons.get(key) or (icons.get(f'trtc_{line_id}') if system == 'NTMC' else '')



def train_badge(system: str, line_id: str, kind) -> str:
    """Express / commuter badge for Airport MRT trains of a known type, else the line's badge."""
    if kind in TRAIN_TYPES:
        emoji, text = TRAIN_TYPES[kind]
        return icons.get(emoji) or f'{line_badge(system, line_id)} {text}'.strip()
    return line_badge(system, line_id)


def logo(system: str) -> str | None:
    return icons.url(f'brand_{system.lower()}')


def alert_embed(system: str, alert: dict) -> discord.Embed:
    normal = alert.get('Status') == 1
    embed = make_embed(f'{"✅" if normal else "⚠️"} {alert.get("Title", "")}',
                       truncate((alert.get('Description') or '').strip(), 3000) or None, theme=THEME,
                       color=SUCCESS if normal else DANGER, stamp=True)
    # Small logo in the author line: a thumbnail would pad short "all normal" notices.
    embed.set_author(name=SYSTEMS[system], icon_url=logo(system))
    lines = [ln.get('LineName', {}).get('Zh_tw') or ln.get('LineID', '')
             for ln in (alert.get('Scope') or {}).get('Lines', [])]
    if lines := [ln for ln in lines if ln]:
        embed.add_field(name='影響路線', value='、'.join(lines), inline=False)
    if not normal and alert.get('PublishTime'):
        embed.add_field(name='發布時間', value=alert['PublishTime'][:16].replace('T', ' '))
    return embed


class Metro(commands.Cog):
    metro = app_commands.Group(name='metro', description='捷運：即時到站、票價、首末班車、營運狀況')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # system -> {station name: [StationID, ...]} (a transfer station has one ID per line)
        self.stations: dict[str, dict[str, list[str]]] = {}
        self._loading: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._loading = asyncio.create_task(self._warm_up())
        if config.RAIL_ALERT_CHANNEL_ID:
            self.alert_loop.change_interval(minutes=config.RAIL_ALERT_MINUTES)
            self.alert_loop.start()

    async def cog_unload(self) -> None:
        self.alert_loop.cancel()
        if self._loading:
            self._loading.cancel()

    # ---- stations ----

    async def _warm_up(self) -> None:
        """Load stations, then pre-fetch every system's run times (cached for a day) in the background,
        so the first /metro route only has to look up fares instead of 5+ TDX calls at once."""
        await self._load_stations()
        for system in SYSTEMS:
            await asyncio.sleep(13)  # stay under the TDX rate limit
            try:
                await tdx.get(f'/v2/Rail/Metro/S2STravelTime/{system}', ttl=86400)
            except Exception:
                log.debug('Could not pre-fetch %s run times', system, exc_info=True)

    async def _load_stations(self) -> None:
        """Station lists rarely change, and TDX allows 5 calls a minute: keep them in the DB for a week."""
        cached = await db.get_kv('metro_stations')
        if cached:
            data = json.loads(cached)
            self.stations = data['stations']
            if time.time() - data['fetched'] < STATION_CACHE_DAYS * 86400:
                return
        stations = {}
        progressive = not self.stations  # nothing cached: let /metro use each system as soon as it arrives
        for system in SYSTEMS:
            # Right after a restart other features hit TDX too, so a rate-limit error is likely:
            # keep retrying this system (with growing pauses) instead of giving up until the next restart.
            delay = 30
            while True:
                try:
                    rows = await tdx.get(f'/v2/Rail/Metro/Station/{system}', **{'$select': 'StationID,StationName'})
                    break
                except Exception:
                    log.warning('Could not load %s stations; retrying in %ds', system, delay, exc_info=True)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 600)
            by_name: dict[str, list[str]] = {}
            for row in rows:
                by_name.setdefault(row['StationName']['Zh_tw'], []).append(row['StationID'])
            stations[system] = by_name
            if progressive:
                self.stations = dict(stations)
            await asyncio.sleep(13)  # stay under the TDX rate limit
        self.stations = stations
        await db.set_kv('metro_stations', json.dumps({'fetched': time.time(), 'stations': stations}, ensure_ascii=False))
        log.info('Loaded metro stations for %d systems', len(stations))

    async def station_autocomplete(self, interaction: discord.Interaction, current: str):
        query = _norm(current)
        matches = [(system, name) for system, names in self.stations.items() for name in names
                   if query in _norm(name)]
        matches.sort(key=lambda m: (_norm(m[1]) != query, len(m[1]), list(SYSTEMS).index(m[0])))
        return [app_commands.Choice(name=f'{name}　{SYSTEMS[system]}', value=f'{system}:{name}')
                for system, name in matches[:25]]

    def resolve(self, text: str) -> tuple[str, str, list[str]] | None:
        """'TRTC:台北車站' (from autocomplete) or a typed name -> (system, name, station IDs)."""
        system, _, name = text.partition(':') if ':' in text else ('', '', text)
        for sys_id in ([system] if system in self.stations else list(self.stations)):
            for station, ids in self.stations.get(sys_id, {}).items():
                if _norm(station) == _norm(name):
                    return sys_id, station, ids
        return None

    async def _stations_or_error(self, interaction: discord.Interaction, *texts: str):
        if not self.stations:
            await interaction.followup.send(embed=error_embed('捷運車站資料還在載入中（bot 剛重啟時約需 1 分鐘），請稍後再試。'))
            return None
        found = [self.resolve(t) for t in texts]
        if not all(found):
            await interaction.followup.send(embed=error_embed('找不到這個車站，請從建議清單中選擇。'))
            return None
        return found

    # ---- /metro board ----

    @metro.command(name='board')
    @app_commands.describe(station='車站')
    @app_commands.autocomplete(station=station_autocomplete)
    async def board(self, interaction: discord.Interaction, station: str):
        """車站即時到站：各方向下一班還有幾分鐘"""
        await interaction.response.defer()
        found = await self._stations_or_error(interaction, station)
        if not found:
            return
        system, name, ids = found[0]
        if system not in LIVE_SYSTEMS:
            await interaction.followup.send(embed=error_embed(
                f'{SYSTEMS[system]}沒有提供即時到站資料（只有台北、高雄、桃園機場捷運有）。'))
            return
        rows = [r for r in await tdx.get(f'/v2/Rail/Metro/LiveBoard/{system}', ttl=30) if r.get('StationID') in ids]
        if not rows:
            # Taipei's feed is not a countdown: it only lists trains arriving at a station right now.
            hint = ('台北捷運的即時資料只會列出「正在進站」的列車，這一站此刻沒有。' if system == 'TRTC'
                    else '目前沒有這一站的即將進站資料。')
            await interaction.followup.send(embed=error_embed(hint))
            return
        types = await self._train_types(ids, rows) if system == 'TYMC' else {}
        # One block per line and direction; inside it one train per row, soonest first:
        # `HH:MM`　badge　countdown — the fixed-width clock keeps every row lined up.
        now = datetime.datetime.now(config.TZ)
        groups: dict[tuple[str, str], list[str]] = {}
        for r in sorted(rows, key=lambda r: r['EstimateTime'] if r.get('EstimateTime') is not None else -1):
            eta, status = r.get('EstimateTime'), r.get('ServiceStatus') or 0
            if status not in SERVICE_STATUS and (eta is None or eta > BOARD_MINUTES):
                continue
            if status in SERVICE_STATUS:
                clock, when = '--:--', SERVICE_STATUS[status]
            else:
                clock = f'{now + datetime.timedelta(minutes=eta):%H:%M}'
                when = '**進站中**' if eta == 0 else f'{eta} 分鐘後'
            dest = r.get('TripHeadSign') or f'往{(r.get("DestinationStationName") or {}).get("Zh_tw", "")}'
            line_id = r.get('LineID', '')
            groups.setdefault((line_id, dest), []).append(
                f'`{clock}`　{train_badge(system, line_id, types.get(id(r)))}　{when}')
        if not groups:
            await interaction.followup.send(embed=error_embed(f'{BOARD_MINUTES} 分鐘內沒有列車進站。'))
            return
        embed = make_embed(f'🚇 {station_title(name)} 即時到站',
                           f'-# {SYSTEMS[system]} · {BOARD_MINUTES} 分鐘內的列車 · 時刻為預估到站時間',
                           theme=THEME, thumbnail=logo(system), stamp=True)
        several_lines = len({line_id for line_id, _ in groups}) > 1
        for (line_id, dest), trains in sorted(groups.items()):
            title = f'{line_name(system, line_id)}　{dest}' if several_lines else dest
            embed.add_field(name=f'▸ {title}', value='\n'.join(trains), inline=False)
        await interaction.followup.send(embed=embed)

    async def _train_types(self, ids: list[str], rows: list[dict]) -> dict[int, int]:
        """The live board doesn't say express or commuter, so match each train to the nearest scheduled one
        (same destination, within 3 minutes) in today's station timetable. Returns {id(row): TrainType}."""
        try:
            timetables = await tdx.get('/v2/Rail/Metro/StationTimeTable/TYMC', ttl=86400,
                                       **{'$filter': ' or '.join(f"StationID eq '{i}'" for i in ids)})
        except tdx.TDXError:
            return {}
        now = datetime.datetime.now(config.TZ)
        today = WEEKDAY_KEYS[now.weekday()]
        scheduled: dict[str, list[tuple[int, int]]] = {}  # destination -> [(minute of day, train type)]
        for t in timetables:
            if not (t.get('ServiceDay') or {}).get(today, True):
                continue
            dest = t.get('DestinationStationID') or t.get('DestinationStaionID')
            for entry in t.get('Timetables') or []:
                h, mi = entry['ArrivalTime'].split(':')
                scheduled.setdefault(dest, []).append((int(h) * 60 + int(mi), entry.get('TrainType')))
        result, used = {}, set()
        minute_now = now.hour * 60 + now.minute
        for r in sorted(rows, key=lambda r: r.get('EstimateTime') if r.get('EstimateTime') is not None else 999):
            if r.get('EstimateTime') is None:
                continue
            due = minute_now + r['EstimateTime']
            dest = r.get('DestinationStationID') or r.get('DestinationStaionID')
            candidates = [(abs((m - due + 720) % 1440 - 720), m, kind) for m, kind in scheduled.get(dest, [])
                          if (dest, m) not in used]
            if candidates:
                gap, m, kind = min(candidates)
                if gap <= 3:
                    used.add((dest, m))
                    result[id(r)] = kind
        return result

    # ---- /metro route ----

    async def _fare(self, system: str, origin_ids: list[str], dest_ids: list[str]) -> tuple[int | None, int | None]:
        origins = ' or '.join(f"OriginStationID eq '{i}'" for i in origin_ids)
        dests = ' or '.join(f"DestinationStationID eq '{i}'" for i in dest_ids)
        rows = await tdx.get(f'/v2/Rail/Metro/ODFare/{system}', ttl=86400, **{'$filter': f'({origins}) and ({dests})'})
        if not rows:
            return None, None
        fares = rows[0]['Fares']
        adult = min((f['Price'] for f in fares if f['FareClass'] == 1), default=None)
        concession = min((f['Price'] for f in fares if f['FareClass'] in (3, 4, 5) and not f.get('CitizenCode')),
                         default=None) or min((f['Price'] for f in fares if f['FareClass'] in (3, 4, 5)), default=None)
        return adult, concession

    async def _shortest_path(self, systems: tuple[str, ...], origins: list[str], targets: list[str]):
        """Dijkstra over station-to-station run times. Nodes are 'SYSTEM:StationID'; stations with the same
        name are joined by transfer edges, which is also how one system connects to another.
        Returns (seconds, start node, path) where path is [(line or 'transfer', node), ...] after the start."""
        graph: dict[str, list[tuple[str, int, str]]] = {}
        self._node_names: dict[str, str] = {}
        for system in systems:
            for route in await tdx.get(f'/v2/Rail/Metro/S2STravelTime/{system}', ttl=86400):
                for hop in route.get('TravelTimes', []):
                    a, b = f'{system}:{hop["FromStationID"]}', f'{system}:{hop["ToStationID"]}'
                    cost = (hop.get('RunTime') or 0) + (hop.get('StopTime') or 0)
                    self._node_names[a] = hop['FromStationName']['Zh_tw']
                    self._node_names[b] = hop['ToStationName']['Zh_tw']
                    for x, y in ((a, b), (b, a)):
                        graph.setdefault(x, []).append((y, cost, route['LineID']))
        by_name: dict[str, list[str]] = {}
        for system in systems:
            for name, ids in self.stations.get(system, {}).items():
                by_name.setdefault(_norm(name), []).extend(f'{system}:{i}' for i in ids)
        for nodes in by_name.values():
            for x in nodes:
                for y in nodes:
                    if x != y:
                        cross = x.split(':')[0] != y.split(':')[0]
                        graph.setdefault(x, []).append((y, CROSS_TRANSFER_SECONDS if cross else TRANSFER_SECONDS,
                                                        'transfer'))
        goal = set(targets)
        queue = [(0, node, node, []) for node in origins]
        seen = set()
        while queue:
            cost, node, start, path = heapq.heappop(queue)
            if node in seen:
                continue
            seen.add(node)
            if node in goal:
                return cost, start, path
            for nxt, step, via in graph.get(node, []):
                if nxt not in seen:
                    heapq.heappush(queue, (cost + step, nxt, start, path + [(via, nxt)]))
        return None, None, []

    @staticmethod
    def _segments(start: str, path: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
        """Split a path into one (system, entry StationID, exit StationID) per metro system ridden."""
        segments, entry, prev = [], start, start
        for _, node in path:
            if node.split(':')[0] != prev.split(':')[0]:
                segments.append((prev.split(':')[0], entry.split(':')[1], prev.split(':')[1]))
                entry = node
            prev = node
        segments.append((prev.split(':')[0], entry.split(':')[1], prev.split(':')[1]))
        return segments

    @metro.command(name='route')
    @app_commands.describe(origin='起站', destination='訖站')
    @app_commands.autocomplete(origin=station_autocomplete, destination=station_autocomplete)
    async def route(self, interaction: discord.Interaction, origin: str, destination: str):
        """票價、行車時間與轉乘路線"""
        await interaction.response.defer()
        found = await self._stations_or_error(interaction, origin, destination)
        if not found:
            return
        (sys_a, name_a, ids_a), (sys_b, name_b, ids_b) = found
        if name_a == name_b and sys_a == sys_b:
            await interaction.followup.send(embed=error_embed('起訖站不能相同。'))
            return
        # Taipei, New Taipei and the Airport MRT connect to each other; Kaohsiung and Taichung stand alone.
        systems = NORTH_SYSTEMS if sys_a in NORTH_SYSTEMS and sys_b in NORTH_SYSTEMS else (sys_a,)
        if sys_b not in systems:
            await interaction.followup.send(embed=error_embed(
                f'{SYSTEMS[sys_a]}和{SYSTEMS[sys_b]}沒有相連，沒辦法搭捷運直接到。'))
            return
        # A station shared by two systems (e.g. 新北產業園區 on the Airport MRT and the Circular line) is one
        # place: start from / arrive at any of its platforms, so there's no pointless transfer at either end.
        def platforms(name: str) -> list[str]:
            return [f'{s}:{i}' for s in systems for n, ids in self.stations.get(s, {}).items()
                    if _norm(n) == _norm(name) for i in ids]

        seconds, start, path = await self._shortest_path(systems, platforms(name_a), platforms(name_b))
        if seconds is None:
            await interaction.followup.send(embed=error_embed('查不到這兩站之間的路線。'))
            return
        segments = self._segments(start, path)

        # Fares are per system: add up each system's segment.
        adult = concession = 0
        for system, entry, exit_ in segments:
            seg_adult, seg_concession = await self._fare(system, [entry], [exit_])
            if seg_adult is None:
                adult = concession = None
                break
            adult += seg_adult
            concession += seg_concession or seg_adult

        names = self._node_names
        transfers = [names.get(node, node) for via, node in path if via == 'transfer']
        used = list(dict.fromkeys(s for s, _, _ in segments))
        embed = make_embed(f'🚇 {name_a} → {name_b}', f'-# {"、".join(SYSTEMS[s] for s in used)}', theme=THEME,
                           thumbnail=logo(used[0]))
        if adult is not None:
            value = f'全票 **NT${adult}**' + (f'\n優待 NT${concession}' if concession else '')
            if len(segments) > 1:
                value += '\n-# 各系統票價相加，實際可能有轉乘優惠'
            embed.add_field(name='票價', value=value)
        embed.add_field(name='行車時間', value=f'約 **{round(seconds / 60)}** 分鐘' +
                        (f'\n轉乘 {len(transfers)} 次' if transfers else '\n不用轉乘'))
        # Summarise as "line → transfer station → line …"
        legs, current = [], None
        for via, node in path:
            if via == 'transfer':
                legs.append(f'在 **{names.get(node, node)}** 轉乘')
                current = None
            elif via != current:
                system = node.split(':')[0]
                legs.append(f'{line_badge(system, via)} {line_name(system, via)}'.strip())
                current = via
        embed.add_field(name='路線', value=' → '.join(legs) or '—', inline=False)
        embed.description += '\n-# 行車時間為表定估算，不含候車；轉乘每次約抓 5 分鐘（跨系統 10 分鐘）'
        await interaction.followup.send(embed=embed)

    # ---- /metro firstlast ----

    @metro.command(name='firstlast')
    @app_commands.describe(station='車站')
    @app_commands.autocomplete(station=station_autocomplete)
    async def firstlast(self, interaction: discord.Interaction, station: str):
        """車站各方向的首班車與末班車（今天）"""
        await interaction.response.defer()
        found = await self._stations_or_error(interaction, station)
        if not found:
            return
        system, name, ids = found[0]
        stations_filter = ' or '.join(f"StationID eq '{i}'" for i in ids)
        rows = await tdx.get(f'/v2/Rail/Metro/FirstLastTimetable/{system}', ttl=86400, **{'$filter': stations_filter})
        today = WEEKDAY_KEYS[datetime.datetime.now(config.TZ).weekday()]
        rows = [r for r in rows if (r.get('ServiceDay') or {}).get(today, True)]
        if not rows:
            await interaction.followup.send(embed=error_embed('查不到這一站的首末班車資料。'))
            return
        embed = make_embed(f'🚇 {station_title(name)} 首末班車', f'-# {SYSTEMS[system]} · 今天', theme=THEME,
                           thumbnail=logo(system))
        seen = set()
        for r in sorted(rows, key=lambda r: (r.get('LineID', ''), r.get('TripHeadSign', ''))):
            dest = r.get('TripHeadSign') or f'往{(r.get("DestinationStationName") or {}).get("Zh_tw", "")}'
            key = (r.get('LineID'), dest)
            if key in seen:
                continue
            seen.add(key)
            embed.add_field(name=f'{line_name(system, r.get("LineID", ""))}　{dest}',
                            value=f'{line_badge(system, r.get("LineID", ""))} 首班 **{r["FirstTrainTime"]}**　'
                                  f'末班 **{r["LastTrainTime"]}**'.strip(), inline=False)
        await interaction.followup.send(embed=embed)

    # ---- /metro alert + push ----

    async def _abnormal(self, system: str) -> list[dict]:
        data = await tdx.get(f'/v2/Rail/Metro/Alert/{system}', ttl=60)
        return [a for a in data.get('Alerts', []) if a.get('Status') != 1]

    @metro.command(name='alert')
    async def alert(self, interaction: discord.Interaction):
        """各捷運系統目前的營運狀況"""
        await interaction.response.defer()
        embeds, normal = [], []
        for system in ALERT_SYSTEMS:
            try:
                abnormal = await self._abnormal(system)
            except tdx.TDXError:
                continue
            if abnormal:
                embeds += [alert_embed(system, a) for a in abnormal[:2]]
            else:
                normal.append(SYSTEMS[system])
        if normal:
            embeds.append(make_embed('✅ 正常營運', '、'.join(normal), theme=THEME, color=SUCCESS, stamp=True))
        await interaction.followup.send(embeds=embeds[:10])

    @tasks.loop(minutes=5)
    @guarded
    async def alert_loop(self):
        channel = self.bot.get_channel(config.RAIL_ALERT_CHANNEL_ID or 0)
        if not channel:
            return
        for system in ALERT_SYSTEMS:
            try:
                abnormal = await self._abnormal(system)
            except Exception:
                log.debug('Metro alert poll failed for %s', system, exc_info=True)
                continue
            # Keyed on content, not PublishTime: TDX re-publishes the same alert every minute.
            state = '|'.join(f'{a.get("AlertID")}:{a.get("Title")}:{a.get("Description")}' for a in abnormal) or 'normal'
            key = f'metro_alert_state:{system}'
            last = await db.get_kv(key)
            if state != last:
                await db.set_kv(key, state)
                if abnormal:
                    await channel.send(embeds=[alert_embed(system, a) for a in abnormal[:5]])
                elif last not in (None, 'normal'):
                    await channel.send(embed=alert_embed(system, {'Title': '已恢復正常營運', 'Status': 1}))
            await asyncio.sleep(13)  # spread the calls out for the TDX rate limit

    @alert_loop.before_loop
    async def before_alert(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    if not (config.TDX_ID and config.TDX_SECRET):
        raise FeatureDisabled('TDX_ID / TDX_SECRET not set')
    await bot.add_cog(Metro(bot))
