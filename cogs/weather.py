import datetime
import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import FeatureDisabled, db, http, icons
from core.embeds import QUAKE, WEATHER as THEME, error_embed, make_embed, timestamp

log = logging.getLogger(__name__)

CWA_API = 'https://opendata.cwa.gov.tw/api/v1/rest/datastore'
City = Literal['臺北市', '新北市', '基隆市', '桃園市', '新竹市', '新竹縣', '苗栗縣', '臺中市', '彰化縣', '南投縣',
               '雲林縣', '嘉義市', '嘉義縣', '臺南市', '高雄市', '屏東縣', '宜蘭縣', '花蓮縣', '臺東縣', '澎湖縣',
               '金門縣', '連江縣']
WEATHER_ICONS = [('雷', '⛈️'), ('雪', '🌨️'), ('雨', '🌧️'), ('陰', '☁️'), ('多雲', '⛅'), ('晴', '☀️')]
QUAKE_COLORS = {'綠色': 0x4CAF50, '黃色': 0xF2C200, '橙色': 0xFF8C00, '紅色': 0xE03C31}


def origin_time(quake: dict) -> datetime.datetime:
    """CWA sends '2026-10-24T09:25:57+08:00' (older docs show '2026-10-24 09:25:57')."""
    parsed = datetime.datetime.fromisoformat(quake['EarthquakeInfo']['OriginTime'].replace(' ', 'T'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=config.TZ)


def period_label(start: datetime.datetime) -> str:
    """'今天白天' / '今晚' / '明天白天' / '明晚' / '明天凌晨'…"""
    offset = (start.date() - datetime.datetime.now(config.TZ).date()).days
    day = {0: '今天', 1: '明天', 2: '後天'}.get(offset, f'{start.month}/{start.day}')
    if start.hour < 6:
        return f'{day}凌晨'
    if start.hour < 18:
        return f'{day}白天'
    return {'今天': '今晚', '明天': '明晚'}.get(day, f'{day}晚上')


def weather_icon(description: str) -> str:
    return next((icon for word, icon in WEATHER_ICONS if word in description), '🌡️')


class Weather(commands.Cog):
    weather = app_commands.Group(name='weather', description='天氣預報與地震報告')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.daily_forecast.start()
        self.quake_loop.start()

    async def cog_unload(self) -> None:
        self.daily_forecast.cancel()
        self.quake_loop.cancel()

    async def cwa(self, dataset: str, **params) -> dict:
        data = await http.get_json(f'{CWA_API}/{dataset}', params={'Authorization': config.CWA_KEY, **params})
        if str(data.get('success')).lower() != 'true':
            raise RuntimeError(f'CWA error: {data}')
        return data['records']

    # ---- forecast ----

    async def forecast_embed(self, city: str) -> discord.Embed:
        records = await self.cwa('F-C0032-001', locationName=city)
        location = records['location'][0]
        elements = {e['elementName']: e['time'] for e in location['weatherElement']}
        periods = []
        for i, period in enumerate(elements['Wx']):
            periods.append({
                'start': datetime.datetime.fromisoformat(period['startTime'].replace(' ', 'T')).replace(tzinfo=config.TZ),
                'end': datetime.datetime.fromisoformat(period['endTime'].replace(' ', 'T')),
                'wx': period['parameter']['parameterName'],
                'low': elements['MinT'][i]['parameter']['parameterName'],
                'high': elements['MaxT'][i]['parameter']['parameterName'],
                'rain': elements['PoP'][i]['parameter']['parameterName'],
                'feel': elements['CI'][i]['parameter']['parameterName'],
            })
        now = periods[0]
        embed = make_embed(f'📍 {city}', theme=THEME, thumbnail=icons.url('brand_cwa'), stamp=True,
                           description=f'-# {period_label(now["start"])} {now["start"]:%H:%M}–{now["end"]:%H:%M}\n'
                                       f'# {weather_icon(now["wx"])} {now["low"]}–{now["high"]}°C\n'
                                       f'**{now["wx"]}** · ☔ 降雨機率 {now["rain"]}% · {now["feel"]}')
        for p in periods[1:]:
            embed.add_field(name=f'{period_label(p["start"])} {p["start"]:%H:%M}–{p["end"]:%H:%M}',
                            value=f'{weather_icon(p["wx"])} **{p["low"]}–{p["high"]}°C**\n{p["wx"]}\n☔ {p["rain"]}% · {p["feel"]}')
        return embed

    @weather.command(name='forecast')
    @app_commands.describe(city='縣市')
    async def forecast(self, interaction: discord.Interaction, city: City):
        """查詢 36 小時天氣預報（中央氣象署）"""
        await interaction.response.defer()
        await interaction.followup.send(embed=await self.forecast_embed(city))

    @tasks.loop(time=config.WEATHER_TIME)
    @guarded
    async def daily_forecast(self):
        channel = self.bot.get_channel(config.WEATHER_CHANNEL_ID or 0)
        if channel:
            embed = await self.forecast_embed(config.WEATHER_CITY)
            embed.title = f'☀️ 早安！{config.WEATHER_CITY}今天的天氣'
            await channel.send(embed=embed)

    # ---- earthquakes ----

    async def latest_quake(self) -> dict | None:
        records = await self.cwa('E-A0015-001', limit=1)
        quakes = records.get('Earthquake') or []
        return quakes[0] if quakes else None

    @staticmethod
    def quake_embed(quake: dict) -> discord.Embed:
        info = quake['EarthquakeInfo']
        magnitude = info['EarthquakeMagnitude']
        origin = origin_time(quake)
        counties: dict[str, str] = {}
        for area in quake.get('Intensity', {}).get('ShakingArea', []):
            # Keep the strongest intensity per county ("4級" > "3級"; "5強" > "5弱").
            for county in (area.get('CountyName') or '').split('、'):
                level = area.get('AreaIntensity', '')
                if county and level > counties.get(county, ''):
                    counties[county] = level
        top = max(counties.values(), default='')
        embed = make_embed(f'🌏 地震報告　第 {quake["EarthquakeNo"]} 號', url=quake.get('Web'), theme=QUAKE,
                           thumbnail=icons.url('brand_cwa'),
                           color=QUAKE_COLORS.get(quake.get('ReportColor'), QUAKE.color),
                           description=f'# 規模 {magnitude["MagnitudeValue"]}' + (f'　最大震度 {top}' if top else '')
                                       + f'\n📍 {info["Epicenter"]["Location"].replace("  ", " ")}\n'
                                       f'🕒 {origin:%m/%d %H:%M:%S}（{timestamp(origin.timestamp(), "R")}）· 深度 {info["FocalDepth"]} 公里')
        if counties:
            ranked = sorted(counties.items(), key=lambda kv: kv[1], reverse=True)
            embed.add_field(name='各地最大震度', value='　'.join(f'{c} **{lv}**' for c, lv in ranked[:20]), inline=False)
        if quake.get('ReportImageURI'):
            embed.set_image(url=quake['ReportImageURI'])
        return embed

    @weather.command(name='quake')
    async def quake(self, interaction: discord.Interaction):
        """最新一筆顯著有感地震報告"""
        await interaction.response.defer()
        quake = await self.latest_quake()
        if not quake:
            await interaction.followup.send(embed=error_embed('目前沒有地震報告。'))
            return
        await interaction.followup.send(embed=self.quake_embed(quake))

    @tasks.loop(minutes=1)
    @guarded
    async def quake_loop(self):
        channel = self.bot.get_channel(config.QUAKE_CHANNEL_ID or 0)
        if not channel:
            return
        try:
            quake = await self.latest_quake()
        except Exception:
            log.debug('Earthquake poll failed', exc_info=True)
            return
        if not quake:
            return
        number = str(quake['EarthquakeNo'])
        last = await db.get_kv('quake_last')
        if number == last:
            return
        await db.set_kv('quake_last', number)
        age = datetime.datetime.now(config.TZ) - origin_time(quake)
        if last is None and age > datetime.timedelta(hours=1):
            return  # first run: remember the latest report without announcing an old quake
        await channel.send(embed=self.quake_embed(quake))

    @daily_forecast.before_loop
    @quake_loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    if not config.CWA_KEY:
        raise FeatureDisabled('CWA_KEY not set')
    await bot.add_cog(Weather(bot))
