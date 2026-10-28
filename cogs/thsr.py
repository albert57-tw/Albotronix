import datetime
import logging
from enum import Enum

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import FeatureDisabled, db, icons, tdx
from core.embeds import DANGER, SUCCESS, THSR as THEME, error_embed, fit_rows, fmt_date, make_embed, \
    paginate_fields, truncate
from core.paginator import send_pages
from core.timeparse import date_autocomplete, now, parse_date

log = logging.getLogger(__name__)

TRAINS_PER_PAGE = 10
DATE_HINT = '日期：YYYY-MM-DD、today、tomorrow 或 +N（N 天後）'


class Station(Enum):
    # Member names are what Discord shows in the station picker.
    南港 = '0990'
    台北 = '1000'
    板橋 = '1010'
    桃園 = '1020'
    新竹 = '1030'
    苗栗 = '1035'
    台中 = '1040'
    彰化 = '1043'
    雲林 = '1047'
    嘉義 = '1050'
    台南 = '1060'
    左營 = '1070'


FARE_CLASS = {1: '全票', 2: '學生', 3: '孩童', 4: '敬老', 5: '愛心', 6: '愛心孩童', 7: '愛心陪伴', 8: '軍警', 9: '孩童／敬老／愛心'}
CABIN_CLASS = {1: '🪑 標準車廂', 2: '💺 商務車廂', 3: '🚶 自由座'}
# TDX TicketType shown by /thsr fare: single tickets named by fare class, group tickets by their own name.
TICKET_TYPES = {1: None, 8: '團體票'}
# College-student discounts on the standard cabin (rounded down to NT$5, like THSR's own fares).
STUDENT_DISCOUNTS = [('88 折', 0.88), ('75 折', 0.75), ('5 折', 0.5)]
SEAT_STATUS = {'O': '🟢', 'L': '🟡', 'X': '🔴'}
SEAT_LEGEND = '-# 🟢 尚有座位　🟡 座位有限　🔴 已售完'
SEAT_WORDS = {'O': '皆有座位', 'L': '座位皆有限', 'X': '皆已售完'}
BOARD_MINUTES = 60
DIRECTION = {0: '南下', 1: '北上'}


def seats(stop: dict) -> str:
    return (f'標準{SEAT_STATUS.get(stop["StandardSeatStatus"], "❔")} '
            f'商務{SEAT_STATUS.get(stop["BusinessSeatStatus"], "❔")}')


def seat_summary(stops: list[dict], key: str) -> str:
    """One cabin class's seats to each stop, with neighbouring stops of the same status merged:
    '🟢 到各站皆有座位' when it's the same all the way, else '🟡 板橋–桃園　🔴 新竹–台中'."""
    runs: list[list] = []  # [status, first stop, last stop]
    for s in stops:
        name = s['StationName']['Zh_tw']
        if runs and runs[-1][0] == s[key]:
            runs[-1][2] = name
        else:
            runs.append([s[key], name, name])
    if len(runs) == 1:
        return f'{SEAT_STATUS.get(runs[0][0], "❔")} 到各站{SEAT_WORDS.get(runs[0][0], "")}'
    return '　'.join(f'{SEAT_STATUS.get(status, "❔")} {first if first == last else f"{first}–{last}"}'
                    for status, first, last in runs)


def is_normal(alert: dict) -> bool:
    """THSR always returns one alert; Status is blank when everything runs normally."""
    return not alert.get('Status') or '正常' in alert.get('Title', '')


def alert_embed(alert: dict) -> discord.Embed:
    normal = is_normal(alert)
    embed = make_embed(f'{"✅" if normal else "⚠️"} {alert.get("Title", "")}',
                       truncate(alert.get('Description') or alert.get('Effect') or '', 3000) or None,
                       theme=THEME, color=SUCCESS if normal else DANGER, stamp=True)
    # Small logo in the author line: a thumbnail would pad short "all normal" notices.
    embed.set_author(name='台灣高鐵', icon_url=icons.url('brand_thsr'))
    if alert.get('Effect') and alert.get('Description'):
        embed.add_field(name='影響', value=truncate(alert['Effect'], 1024), inline=False)
    # The "normal" status is re-published constantly; its time is just the last data refresh, not news.
    if not normal and alert.get('PublishTime') and not alert['PublishTime'].startswith('0001'):
        embed.add_field(name='發布時間', value=alert['PublishTime'][:16].replace('T', ' '))
    return embed


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def travel_minutes(departure: str, arrival: str) -> int:
    return (_minutes(arrival) - _minutes(departure)) % (24 * 60)  # handles trains past midnight


def travel_time(departure: str, arrival: str) -> str:
    """Fixed-width 'H:MM' (1:43), so every row lines up."""
    minutes = travel_minutes(departure, arrival)
    return f'{minutes // 60}:{minutes % 60:02d}'


async def check_pair(interaction: discord.Interaction, origin: Station, destination: Station) -> bool:
    if origin == destination:
        await interaction.response.send_message(embed=error_embed('起訖站不能相同。'), ephemeral=True)
        return False
    return True


class THSR(commands.Cog):
    thsr = app_commands.Group(name='thsr', description='台灣高鐵')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        if config.RAIL_ALERT_CHANNEL_ID:
            self.alert_loop.change_interval(minutes=config.RAIL_ALERT_MINUTES)
            self.alert_loop.start()

    async def cog_unload(self) -> None:
        self.alert_loop.cancel()

    async def free_cars(self, day: str) -> dict[str, str]:
        """TrainNo (without leading zeros) -> non-reserved cars, e.g. '10-12'."""
        try:
            data = await tdx.get(f'/v2/Rail/THSR/DailyFreeSeatingCar/TrainDate/{day}', ttl=86400)
        except tdx.TDXError:
            return {}
        return {c['TrainNo'].lstrip('0'): c['CarConfig'].split(':')[-1].strip()
                for c in data.get('FreeSeatingCars', [])}

    @thsr.command(name='fare')
    @app_commands.describe(origin='起站', destination='訖站')
    async def fare(self, interaction: discord.Interaction, origin: Station, destination: Station):
        """查詢高鐵票價"""
        if not await check_pair(interaction, origin, destination):
            return
        await interaction.response.defer()
        data = await tdx.get(f'/v2/Rail/THSR/ODFare/{origin.value}/to/{destination.value}', ttl=86400)
        embed = make_embed(f'🎫 {origin.name} → {destination.name} 票價', theme=THEME, thumbnail=icons.url('brand_thsr'))
        # Per cabin: one row per price, most expensive first, merging ticket kinds that cost the same.
        by_cabin: dict[int, dict[int, list[str]]] = {}
        for f in data[0]['Fares']:
            if f['TicketType'] in TICKET_TYPES:
                name = TICKET_TYPES[f['TicketType']] or FARE_CLASS.get(f['FareClass'], str(f['FareClass']))
                by_cabin.setdefault(f['CabinClass'], {}).setdefault(f['Price'], []).append(name)
        # TDX has no college-student fares: they are percentages of the standard-cabin adult fare.
        adult = next((f['Price'] for f in data[0]['Fares']
                      if f['TicketType'] == 1 and f['FareClass'] == 1 and f['CabinClass'] == 1), None)
        if adult:
            for label, rate in STUDENT_DISCOUNTS:
                by_cabin.setdefault(1, {}).setdefault(int(adult * rate) // 5 * 5, []).append(f'大學生 {label}')
        for cabin in sorted(by_cabin):
            rows = [f'**NT${price:,}**　{"、".join(names)}' for price, names in sorted(by_cabin[cabin].items(), reverse=True)]
            embed.add_field(name=f'▸ {CABIN_CLASS.get(cabin, str(cabin))}', value='\n'.join(rows), inline=False)
        if adult:
            embed.add_field(name='​', value='-# • 大學生優惠只開放部分車次、名額有限，以高鐵訂票時顯示為準\n'
                                                 '-# • 團體票為 11 人以上同行', inline=False)
        await interaction.followup.send(embed=embed)

    @thsr.command(name='status')
    @app_commands.describe(origin='起站', destination='訖站', date=DATE_HINT)
    @app_commands.autocomplete(date=date_autocomplete)
    async def status(self, interaction: discord.Interaction, origin: Station, destination: Station,
                     date: str = 'today'):
        """查詢高鐵時刻與剩餘座位"""
        train_date = parse_date(date)
        if train_date is None:
            await interaction.response.send_message(embed=error_embed(f'看不懂這個日期。{DATE_HINT}'), ephemeral=True)
            return
        if not await check_pair(interaction, origin, destination):
            return
        await interaction.response.defer()
        day = train_date.isoformat()
        od = f'{origin.value}/to/{destination.value}'
        timetable = list(await tdx.get(f'/v2/Rail/THSR/DailyTimetable/OD/{od}/{day}', ttl=3600))
        if not timetable:
            await interaction.followup.send(embed=error_embed(f'{fmt_date(train_date)} 沒有班次（時刻表約只開放一個月內）。'))
            return
        try:
            seats_data = await tdx.get(f'/v2/Rail/THSR/AvailableSeatStatus/Train/OD/{od}/TrainDate/{day}', ttl=300)
            seat_map = {s['TrainNo']: s for s in seats_data.get('AvailableSeats', [])}
        except tdx.TDXError:
            seat_map = {}
        free = await self.free_cars(day)

        timetable.sort(key=lambda t: t['OriginStopTime']['DepartureTime'])
        if train_date == now().date():
            current = now().strftime('%H:%M')
            timetable = [t for t in timetable if t['OriginStopTime']['DepartureTime'] >= current]
            if not timetable:
                await interaction.followup.send(embed=error_embed('今天已經沒有班次了。'))
                return
        # One field per train (like the original bot), always the same three lines: times, stops, seats.
        fields = []
        for train in timetable:
            no = train['DailyTrainInfo']['TrainNo']
            dep = train['OriginStopTime']['DepartureTime']
            arr = train['DestinationStopTime']['ArrivalTime']
            stops = int(train['DestinationStopTime']['StopSequence']) - int(train['OriginStopTime']['StopSequence']) - 1
            value = [f'**{dep} → {arr}**　行車 **{travel_time(dep, arr)}**',
                     f'停靠 {stops} 站' if stops else '直達車']
            seat_line = [seats(seat)] if (seat := seat_map.get(no)) else []
            if cars := free.get(no.lstrip('0')):
                seat_line.append(f'自由座 {cars} 車')
            if seat_line:
                value.append('　'.join(seat_line))
            fields.append((f'🚄 {origin.name} → {destination.name}　{no} 次', '\n'.join(value)))

        header = f'**{fmt_date(train_date)}** · 共 {len(fields)} 班'
        pages = paginate_fields(fields, TRAINS_PER_PAGE, lambda: make_embed(
            f'🚄 {origin.name} → {destination.name}', f'{header}\n{SEAT_LEGEND}', theme=THEME, thumbnail=icons.url('brand_thsr'),
            stamp=True))
        await send_pages(interaction, pages)

    @thsr.command(name='train')
    @app_commands.describe(train_no='車次，例如 803', date=DATE_HINT)
    @app_commands.autocomplete(date=date_autocomplete)
    async def train(self, interaction: discord.Interaction, train_no: str, date: str = 'today'):
        """查詢某班高鐵的所有停靠站"""
        train_date = parse_date(date)
        if train_date is None or not (train_no.strip().isascii() and train_no.strip().isdigit()):
            await interaction.response.send_message(embed=error_embed(f'請輸入車次號碼和正確的日期。{DATE_HINT}'),
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        no, day = train_no.strip().zfill(4), train_date.isoformat()
        data = await tdx.get(f'/v2/Rail/THSR/DailyTimetable/TrainNo/{no}/TrainDate/{day}', ttl=3600)
        if not data:
            await interaction.followup.send(embed=error_embed(f'{fmt_date(train_date)} 沒有 {train_no} 次列車。'))
            return
        info, stops = data[0]['DailyTrainInfo'], data[0]['StopTimes']
        current = now().strftime('%H:%M') if train_date == now().date() else None
        lines = []
        for stop in stops:
            gone = current and stop['DepartureTime'] < current
            times = stop['DepartureTime'] if stop['ArrivalTime'] == stop['DepartureTime'] \
                else f'{stop["ArrivalTime"]}–{stop["DepartureTime"]}'
            name = stop['StationName']['Zh_tw']
            lines.append(f'-# ▫️ `{times:>11}`　{name}' if gone else f'🔸 `{times:>11}`　**{name}**')
        direction = '南下' if info['Direction'] == 0 else '北上'
        free = (await self.free_cars(day)).get(no.lstrip('0'))
        meta = f'{fmt_date(train_date)} · {direction}' + (f' · 自由座 {free} 車' if free else '')
        embed = make_embed(f'🚄 {info["TrainNo"]} 次　{info["StartingStationName"]["Zh_tw"]} → '
                           f'{info["EndingStationName"]["Zh_tw"]}', f'{meta}\n\n' + '\n'.join(lines),
                           theme=THEME, thumbnail=icons.url('brand_thsr'))
        await interaction.followup.send(embed=embed)

    @thsr.command(name='board')
    @app_commands.describe(station='出發車站')
    async def board(self, interaction: discord.Interaction, station: Station):
        """車站看板：接下來 1 小時南下、北上的班次與即時剩餘座位"""
        await interaction.response.defer()
        data = await tdx.get(f'/v2/Rail/THSR/AvailableSeatStatusList/{station.value}', ttl=120)
        current = now()
        start, end = f'{current:%H:%M}', f'{current + datetime.timedelta(minutes=BOARD_MINUTES):%H:%M}'
        if end < start:
            end = '23:59'  # the window runs past midnight: today's list ends there anyway
        trains = sorted((t for t in data.get('AvailableSeats', []) if start <= t['DepartureTime'] <= end),
                        key=lambda t: t['DepartureTime'])
        if not trains:
            await interaction.followup.send(embed=error_embed(f'接下來 {BOARD_MINUTES} 分鐘沒有班次。'))
            return
        embed = make_embed(f'🚄 {station.name}站 即時看板',
                           f'-# 接下來 {BOARD_MINUTES} 分鐘的班次 · 座位為從本站出發、到各站的剩餘座位\n'
                           f'{SEAT_LEGEND}',
                           theme=THEME, thumbnail=icons.url('brand_thsr'), stamp=True)
        for direction, label in DIRECTION.items():
            rows = []
            for t in (t for t in trains if t.get('Direction') == direction):
                rows.append(f'`{t["DepartureTime"]}`　`{t["TrainNo"]:>4}` 往{t["EndingStationName"]["Zh_tw"]}\n'
                            f'-# 標準　{seat_summary(t["StopStations"], "StandardSeatStatus")}\n'
                            f'-# 商務　{seat_summary(t["StopStations"], "BusinessSeatStatus")}')
            if rows:
                embed.add_field(name=f'▸ {label}', value=fit_rows(rows), inline=False)
        await interaction.followup.send(embed=embed)

    @thsr.command(name='alert')
    async def alert(self, interaction: discord.Interaction):
        """高鐵目前的營運狀況"""
        await interaction.response.defer()
        alerts = await tdx.get('/v2/Rail/THSR/AlertInfo', ttl=60)
        await interaction.followup.send(embeds=[alert_embed(a) for a in alerts[:5]] or
                                        [alert_embed({'Title': '全線營運正常'})])

    @tasks.loop(minutes=5)
    @guarded
    async def alert_loop(self):
        channel = self.bot.get_channel(config.RAIL_ALERT_CHANNEL_ID or 0)
        if not channel:
            return
        try:
            alerts = await tdx.get('/v2/Rail/THSR/AlertInfo', ttl=60)
        except Exception:
            log.debug('THSR alert poll failed', exc_info=True)
            return
        abnormal = [a for a in alerts if not is_normal(a)]
        state = '|'.join(f'{a.get("AlertID")}:{a.get("Title")}:{a.get("Status")}' for a in abnormal) or 'normal'
        last = await db.get_kv('thsr_alert_state')
        if state == last:
            return
        await db.set_kv('thsr_alert_state', state)
        if abnormal:
            await channel.send(embeds=[alert_embed(a) for a in abnormal[:5]])
        elif last not in (None, 'normal'):
            await channel.send(embed=alert_embed({'Title': '高鐵已恢復正常營運'}))

    @alert_loop.before_loop
    async def before_alert(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    if not (config.TDX_ID and config.TDX_SECRET):
        raise FeatureDisabled('TDX_ID / TDX_SECRET not set')
    await bot.add_cog(THSR(bot))
