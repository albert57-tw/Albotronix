import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import FeatureDisabled, db, icons, tdx
from core.embeds import DANGER, SUCCESS, TRA as THEME, error_embed, fmt_date, fit_rows, make_embed, paginate_fields, truncate
from core.paginator import send_pages
from core.timeparse import date_autocomplete, now, parse_date
from cogs.thsr import DATE_HINT, travel_time

log = logging.getLogger(__name__)

TRAINS_PER_PAGE = 10
BOARD_MINUTES = 60
# 11 is missing from TDX's docs; the data shows it is the EMU3000 Tze-Chiang.
TRAIN_TYPES = {1: '太魯閣', 2: '普悠瑪', 3: '自強', 4: '莒光', 5: '復興', 6: '區間', 7: '普快', 10: '區間快',
               11: '自強(3000)'}
ALERT_LEVEL = {1: '🔴 重度', 2: '🟠 中度', 3: '🟡 輕度'}
ALERT_DIRECTION = {0: '南下', 1: '北上', 2: '雙向'}
# Timetable direction: 順行 runs clockwise round the island (Taipei → Keelung / east coast), 逆行 the other way.
DIRECTION = {0: '順行', 1: '逆行'}
LIVE_STATUS = {0: '即將抵達', 1: '停靠在', 2: '剛離開'}


def short_type(name: str) -> str:
    """'自強(3000)(EMU3000 型電車)' -> '自強(3000)', '普悠瑪(普悠瑪)' -> '普悠瑪'. Keeps numeric tags only."""
    return re.sub(r'\((?!\d+\))[^()]*\)', '', name).strip()


# Train type -> application emoji (assets/emojis); 區間快 is checked before 區間.
TYPE_EMOJI = [('太魯閣', 'tra_taroko'), ('普悠瑪', 'tra_puyuma'), ('自強', 'tra_tzechiang'), ('莒光', 'tra_chukuang'),
              ('復興', 'tra_fuhsing'), ('區間快', 'tra_localexp'), ('區間', 'tra_local'), ('普快', 'tra_ordinary')]


def type_badge(name: str) -> str:
    """Coloured train-type badge, falling back to the short type name if the emoji isn't available."""
    short = short_type(name)
    key = next((emoji for word, emoji in TYPE_EMOJI if word in short), None)
    badge = icons.get(key) if key else ''
    # Keep a "(3000)" style tag visible next to the badge: it tells the EMU3000 apart from older 自強.
    tag = re.search(r'\(\d+\)', short)
    return f'{badge}{tag.group(0) if tag else ""}' if badge else short


def delay_text(minutes: int | None) -> str:
    if minutes is None:
        return ''
    return '✅ 準點' if minutes == 0 else f'⚠️ 誤點 {minutes} 分'


def alert_embed(alert: dict) -> discord.Embed:
    normal = alert.get('Status') == 1
    embed = make_embed(f'{"✅" if normal else "⚠️"} {alert.get("Title", "")}',
                       truncate((alert.get('Description') or '').strip(), 3000) or None, theme=THEME,
                       url=alert.get('AlertURL') or None, color=SUCCESS if normal else DANGER, stamp=True)
    # Small logo in the author line: a thumbnail would pad short "all normal" notices and squeeze the fields.
    embed.set_author(name='臺鐵公司', icon_url=icons.url('brand_tra'))
    if alert.get('Level') in ALERT_LEVEL:
        embed.add_field(name='影響程度', value=ALERT_LEVEL[alert['Level']])
    if alert.get('Direction') in ALERT_DIRECTION:
        embed.add_field(name='方向', value=ALERT_DIRECTION[alert['Direction']])
    if alert.get('EndTime'):
        embed.add_field(name='預計結束', value=alert['EndTime'][5:16].replace('T', ' ').replace('-', '/'))
    lines = [ln['LineName'] for ln in (alert.get('Scope') or {}).get('Lines', []) if ln.get('LineName')]
    if lines:
        embed.add_field(name='影響路線', value='、'.join(lines), inline=False)
    return embed


def _normalize(name: str) -> str:
    return name.replace('台', '臺').lower()


class TRA(commands.Cog):
    tra = app_commands.Group(name='tra', description='台鐵')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.stations: dict[str, dict] = {}  # StationID -> StationName {Zh_tw, En}

    async def cog_load(self) -> None:
        try:
            await self._load_stations()
        except Exception:
            log.warning('Could not preload TRA stations; will retry on first use', exc_info=True)
        if config.RAIL_ALERT_CHANNEL_ID:
            self.alert_loop.change_interval(minutes=config.RAIL_ALERT_MINUTES)
            self.alert_loop.start()

    async def cog_unload(self) -> None:
        self.alert_loop.cancel()

    async def _load_stations(self) -> None:
        data = await tdx.get('/v3/Rail/TRA/Station', ttl=86400, **{'$select': 'StationID,StationName'})
        self.stations = {s['StationID']: s['StationName'] for s in data['Stations']}

    async def live_board(self) -> list[dict]:
        return (await tdx.get('/v3/Rail/TRA/TrainLiveBoard', ttl=60)).get('TrainLiveBoards', [])

    def station_name(self, station_id: str) -> str:
        name = self.stations.get(station_id)
        return name['Zh_tw'] if name else station_id

    async def station_autocomplete(self, interaction: discord.Interaction, current: str):
        if not self.stations:
            try:
                await self._load_stations()
            except Exception:
                return []
        query = _normalize(current)
        matches = [(sid, n) for sid, n in self.stations.items()
                   if query in _normalize(n['Zh_tw']) or query in n['En'].lower() or query == sid]
        # Exact name matches first, then shorter names (main stations tend to have short names).
        matches.sort(key=lambda m: (_normalize(m[1]['Zh_tw']) != query, len(m[1]['Zh_tw'])))
        return [app_commands.Choice(name=f'{n["Zh_tw"]}　{n["En"]}', value=sid) for sid, n in matches[:25]]

    async def _resolve_station(self, text: str) -> str | None:
        """Accept a StationID from autocomplete, or a typed station name."""
        if not self.stations:
            await self._load_stations()
        if text in self.stations:
            return text
        query = _normalize(text)
        for sid, n in self.stations.items():
            if query in (_normalize(n['Zh_tw']), n['En'].lower()):
                return sid
        return None

    async def _stations_or_error(self, interaction: discord.Interaction, *names: str) -> list[str] | None:
        ids = [await self._resolve_station(n) for n in names]
        if not all(ids):
            await interaction.followup.send(embed=error_embed('找不到這個車站，請從建議清單中選擇。'))
            return None
        return ids

    @tra.command(name='timetable')
    @app_commands.describe(origin='起站', destination='訖站', date=DATE_HINT)
    @app_commands.autocomplete(origin=station_autocomplete, destination=station_autocomplete, date=date_autocomplete)
    async def timetable(self, interaction: discord.Interaction, origin: str, destination: str, date: str = 'today'):
        """查詢台鐵時刻表（今天的班次附即時誤點）"""
        await interaction.response.defer()
        ids = await self._stations_or_error(interaction, origin, destination)
        if not ids:
            return
        train_date = parse_date(date)
        if train_date is None:
            await interaction.followup.send(embed=error_embed(f'看不懂這個日期。{DATE_HINT}'))
            return
        data = await tdx.get(f'/v3/Rail/TRA/DailyTrainTimetable/OD/{ids[0]}/to/{ids[1]}/{train_date.isoformat()}',
                             ttl=3600)
        trains = data.get('TrainTimetables', [])
        if not trains:
            await interaction.followup.send(embed=error_embed(f'{fmt_date(train_date)} 沒有直達的班次。'))
            return

        delays: dict[str, int] = {}
        is_today = train_date == now().date()
        if is_today:
            try:
                delays = {t['TrainNo']: t['DelayTime'] for t in await self.live_board()}
            except tdx.TDXError:
                pass
        current = now()
        current_minutes = current.hour * 60 + current.minute

        rows = []
        for train in trains:
            stops = train['StopTimes']
            dep, arr = stops[0]['DepartureTime'], stops[-1]['ArrivalTime']
            if is_today:
                h, m = map(int, dep.split(':'))
                if h * 60 + m + delays.get(train['TrainInfo']['TrainNo'], 0) < current_minutes:
                    continue  # already left the origin station, even counting its delay
            rows.append((dep, train, arr))
        rows.sort(key=lambda r: r[0])
        if not rows:
            await interaction.followup.send(embed=error_embed('今天已經沒有班次了。'))
            return

        # One block per train, like the THSR timetable.
        fields = []
        for dep, train, arr in rows:
            info = train['TrainInfo']
            no = info['TrainNo']
            # The type badge is an emoji, and emojis don't render in field names, so it leads the value.
            value = [f'{type_badge(info["TrainTypeName"]["Zh_tw"])}　**{dep} → {arr}**　行車 **{travel_time(dep, arr)}**']
            if info.get('SuspendedFlag'):
                value.append('🚫 停駛')
            elif no in delays:
                value.append(delay_text(delays[no]))
            fields.append((f'🚆 {short_type(info["TrainTypeName"]["Zh_tw"])} {no} 次', '\n'.join(value)))
        header = f'**{fmt_date(train_date)}** · 共 {len(fields)} 班'
        title = f'🚆 {self.station_name(ids[0])} → {self.station_name(ids[1])}'
        pages = paginate_fields(fields, TRAINS_PER_PAGE,
                                lambda: make_embed(title, header, theme=THEME, thumbnail=icons.url('brand_tra'), stamp=is_today))
        await send_pages(interaction, pages)

    @tra.command(name='train')
    @app_commands.describe(train_no='車次，例如 152', date=DATE_HINT)
    @app_commands.autocomplete(date=date_autocomplete)
    async def train(self, interaction: discord.Interaction, train_no: str, date: str = 'today'):
        """查詢某班台鐵的停靠站、目前位置與誤點"""
        train_date = parse_date(date)
        no = train_no.strip()
        if train_date is None or not (no.isascii() and no.isdigit()):
            await interaction.response.send_message(embed=error_embed(f'請輸入車次號碼和正確的日期。{DATE_HINT}'),
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        is_today = train_date == now().date()
        if is_today:
            data = await tdx.get(f'/v3/Rail/TRA/DailyTrainTimetable/Today/TrainNo/{no}', ttl=3600)
        else:
            data = await tdx.get(f'/v3/Rail/TRA/DailyTrainTimetable/TrainDate/{train_date.isoformat()}', ttl=3600,
                                 **{'$filter': f"TrainInfo/TrainNo eq '{no}'"})
        timetables = data.get('TrainTimetables', [])
        if not timetables:
            await interaction.followup.send(embed=error_embed(f'{fmt_date(train_date)} 沒有 {no} 次列車。'))
            return
        info, stops = timetables[0]['TrainInfo'], timetables[0]['StopTimes']

        live = None
        if is_today:
            try:
                live = next((b for b in await self.live_board() if b['TrainNo'] == no), None)
            except tdx.TDXError:
                pass
        live_ids = [s['StationID'] for s in stops]
        live_index = live_ids.index(live['StationID']) if live and live['StationID'] in live_ids else None
        lines = []
        for i, stop in enumerate(stops):
            times = stop['DepartureTime'] if stop['ArrivalTime'] == stop['DepartureTime'] \
                else f'{stop["ArrivalTime"]}–{stop["DepartureTime"]}'
            name = stop['StationName']['Zh_tw']
            if live_index is not None and i == live_index:
                lines.append(f'📍 `{times:>11}`　**{name}**')
            elif live_index is not None and i < live_index:
                lines.append(f'-# ▫️ `{times:>11}`　{name}')
            else:
                lines.append(f'🔹 `{times:>11}`　{name}')

        meta = f'{fmt_date(train_date)} · {info.get("TripHeadSign") or "往" + info["EndingStationName"]["Zh_tw"]}'
        if live:
            meta += (f'\n**目前{LIVE_STATUS.get(live.get("TrainStationStatus"), "位於")} '
                     f'{live["StationName"]["Zh_tw"]}** · {delay_text(live.get("DelayTime", 0))}')
        elif is_today:
            meta += '\n-# 目前不在行駛中'
        embed = make_embed(f'🚆 {short_type(info["TrainTypeName"]["Zh_tw"])} {no} 次　'
                           f'{info["StartingStationName"]["Zh_tw"]} → {info["EndingStationName"]["Zh_tw"]}',
                           theme=THEME, thumbnail=icons.url('brand_tra'), stamp=bool(live))
        embed.description = meta + '\n\n' + truncate('\n'.join(lines), 4000 - len(meta))
        if info.get('Note'):
            embed.add_field(name='備註', value=truncate(info['Note'], 1024), inline=False)
        await interaction.followup.send(embed=embed)

    @tra.command(name='board')
    @app_commands.describe(station='車站')
    @app_commands.autocomplete(station=station_autocomplete)
    async def board(self, interaction: discord.Interaction, station: str):
        """車站看板：接下來 1 小時各方向的列車與誤點"""
        await interaction.response.defer()
        ids = await self._stations_or_error(interaction, station)
        if not ids:
            return
        # The live board only covers the next few trains, so the list comes from today's station timetable,
        # with delays from the train live board and platforms / cancellations from the station live board.
        data = await tdx.get(f'/v3/Rail/TRA/DailyStationTimetable/Today/Station/{ids[0]}', ttl=3600)
        try:
            delays = {t['TrainNo']: t['DelayTime'] for t in await self.live_board()}
        except tdx.TDXError:
            delays = {}
        try:
            live = {b['TrainNo']: b for b in (await tdx.get(f'/v3/Rail/TRA/StationLiveBoard/Station/{ids[0]}',
                                                            ttl=30)).get('StationLiveBoards', [])}
        except tdx.TDXError:
            live = {}
        current = now()
        current_minutes = current.hour * 60 + current.minute
        embed = make_embed(f'🚉 {self.station_name(ids[0])}站 即時看板',
                           f'-# 接下來 {BOARD_MINUTES} 分鐘的列車 · 時刻為表定發車時間', theme=THEME,
                           thumbnail=icons.url('brand_tra'), stamp=True)
        for st in sorted(data.get('StationTimetables', []), key=lambda s: s.get('Direction', 0)):
            rows, destinations = [], []
            for t in sorted(st.get('TimeTables', []), key=lambda t: t['DepartureTime']):
                no = t['TrainNo']
                h, m = map(int, t['DepartureTime'].split(':'))
                departs = h * 60 + m
                # Skip trains already gone (counting their delay) or beyond the window.
                if departs + delays.get(no, 0) < current_minutes or departs > current_minutes + BOARD_MINUTES:
                    continue
                board = live.get(no, {})
                if t.get('SuspendedFlag') or board.get('RunningStatus') == 2:
                    status = '🚫 停駛'
                else:
                    status = delay_text(delays[no] if no in delays else board.get('DelayTime'))
                platform = f' · {board["Platform"]} 月台' if board.get('Platform') else ''
                dest = t['DestinationStationName']['Zh_tw']
                destinations.append(dest)
                rows.append(f'`{t["DepartureTime"]}`　{type_badge(t["TrainTypeName"]["Zh_tw"])}　'
                            f'`{no:>4}` 往{dest}　{status}{platform}'.rstrip('　'))
            if rows:
                heading = '、'.join(list(dict.fromkeys(destinations))[:3])
                embed.add_field(name=f'▸ {DIRECTION.get(st.get("Direction"), "")}　往 {heading} 方向',
                                value=fit_rows(rows), inline=False)
        if not embed.fields:
            await interaction.followup.send(embed=error_embed(f'接下來 {BOARD_MINUTES} 分鐘沒有列車。'))
            return
        await interaction.followup.send(embed=embed)

    @tra.command(name='fare')
    @app_commands.describe(origin='起站', destination='訖站')
    @app_commands.autocomplete(origin=station_autocomplete, destination=station_autocomplete)
    async def fare(self, interaction: discord.Interaction, origin: str, destination: str):
        """查詢台鐵各車種票價"""
        await interaction.response.defer()
        ids = await self._stations_or_error(interaction, origin, destination)
        if not ids:
            return
        data = await tdx.get(f'/v3/Rail/TRA/ODFare/{ids[0]}/to/{ids[1]}', ttl=86400)
        # One entry per train type and route (e.g. via the mountain or coast line): keep the cheapest.
        best: dict[int, dict] = {}
        for od in data.get('ODFares', []):
            prices = {f['FareClass']: f['Price'] for f in od['Fares'] if f['TicketType'] == 1}
            if 1 not in prices:
                continue
            current = best.get(od['TrainType'])
            if not current or prices[1] < current['adult']:
                best[od['TrainType']] = {'adult': prices[1], 'half': prices.get(3) or prices.get(4),
                                         'km': od.get('TravelDistance')}
        if not best:
            await interaction.followup.send(embed=error_embed('查不到這兩站之間的票價。'))
            return
        km = min((f['km'] for f in best.values() if f['km']), default=None)
        # Many train types cost the same: one line per price, listing the types.
        by_price: dict[tuple, list[str]] = {}
        for train_type, f in best.items():
            by_price.setdefault((f['adult'], f['half']), []).append(type_badge(TRAIN_TYPES.get(train_type, str(train_type))))
        lines = [f'**NT${adult:,}**　{" ".join(types)}' + (f'\n-# 孩童／敬老／愛心 NT${half:,}' if half else '')
                 for (adult, half), types in sorted(by_price.items(), reverse=True)]
        embed = make_embed(f'🎫 {self.station_name(ids[0])} → {self.station_name(ids[1])} 票價',
                           '\n'.join(lines) + (f'\n\n-# 全票價格 · 距離 {km} 公里' if km else ''),
                           theme=THEME, thumbnail=icons.url('brand_tra'))
        await interaction.followup.send(embed=embed)

    async def alerts(self) -> list[dict]:
        data = await tdx.get('/v3/Rail/TRA/Alert', ttl=60)
        return data.get('Alerts', [])

    @tra.command(name='alert')
    async def alert(self, interaction: discord.Interaction):
        """台鐵目前的營運異常（停駛、延誤）"""
        await interaction.response.defer()
        active = [a for a in await self.alerts() if a.get('Status') != 1]
        if not active:
            await interaction.followup.send(embed=alert_embed({'Title': '台鐵全線營運正常', 'Status': 1}))
            return
        await interaction.followup.send(embeds=[alert_embed(a) for a in active[:5]])

    @tasks.loop(minutes=5)
    @guarded
    async def alert_loop(self):
        channel = self.bot.get_channel(config.RAIL_ALERT_CHANNEL_ID or 0)
        if not channel:
            return
        try:
            active = [a for a in await self.alerts() if a.get('Status') != 1]
        except Exception:
            log.debug('TRA alert poll failed', exc_info=True)
            return
        keys = {f'{a["AlertID"]}:{a.get("PublishTime", "")}': a for a in active}
        new = await db.filter_unseen('tra_alert', list(keys))
        if new:
            for key in new[:5]:
                await channel.send(embed=alert_embed(keys[key]))
            await db.mark_seen('tra_alert', new, time.time())

    @alert_loop.before_loop
    async def before_alert(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    if not (config.TDX_ID and config.TDX_SECRET):
        raise FeatureDisabled('TDX_ID / TDX_SECRET not set')
    await bot.add_cog(TRA(bot))
