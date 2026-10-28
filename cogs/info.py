"""/info: exchange rates, fuel prices and CVE lookups, plus the daily CISA KEV / critical-CVE push."""
import asyncio
import datetime
import html
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.loops import guarded
from core import db, http
from core.embeds import FINANCE, SECURITY, timestamp, error_embed, make_embed, truncate

log = logging.getLogger(__name__)

NVD_API = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
KEV_FEED = 'https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json'
SEVERITY_COLOR = {'CRITICAL': 0x8B0000, 'HIGH': 0xE03C31, 'MEDIUM': 0xF2A900, 'LOW': 0x4CAF50}
SEVERITY_LABEL = {'CRITICAL': '🟣 嚴重', 'HIGH': '🔴 高', 'MEDIUM': '🟠 中', 'LOW': '🟢 低'}
CVE_RE = re.compile(r'CVE-\d{4}-\d{4,}', re.I)

# The Bank of Taiwan rate page sits behind a bot challenge, so this uses ExchangeRate-API's
# free daily mid-market rates instead (no key needed).
RATES_URL = 'https://open.er-api.com/v6/latest/TWD'
CPC_URL = 'https://vipmbr.cpc.com.tw/CPCSTN/ListPriceWebService.asmx/getCPCMainProdListPrice'
CPC_HISTORY_URL = 'https://vipmbr.cpc.com.tw/mbwebs/showhistoryprice_oil.aspx'
CACHE_SECONDS = 3600
# Product name in the price list -> its column in the history table.
HISTORY_COLUMN = {'92無鉛汽油': '無鉛汽油92', '95無鉛汽油': '無鉛汽油95', '98無鉛汽油': '無鉛汽油98', '超級柴油': '超級/高級柴油'}

CURRENCIES = {
    'USD': ('🇺🇸', '美元'), 'JPY': ('🇯🇵', '日圓'), 'EUR': ('🇪🇺', '歐元'), 'CNY': ('🇨🇳', '人民幣'),
    'KRW': ('🇰🇷', '韓元'), 'HKD': ('🇭🇰', '港幣'), 'GBP': ('🇬🇧', '英鎊'), 'AUD': ('🇦🇺', '澳幣'),
    'SGD': ('🇸🇬', '新加坡幣'), 'CAD': ('🇨🇦', '加拿大幣'), 'CHF': ('🇨🇭', '瑞士法郎'), 'THB': ('🇹🇭', '泰銖'),
    'VND': ('🇻🇳', '越南盾'),
}


def fmt_rate(value: float) -> str:
    """Five significant digits: 32.59, 4.8144, 0.20771, 0.0012346."""
    return f'{value:,.5g}'


class NVDLimiter:
    """NVD allows 5 requests / 30s without a key (50 with one)."""

    def __init__(self):
        self.interval = 0.6 if config.NVD_API_KEY else 6
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        async with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


class Info(commands.Cog):
    info = app_commands.Group(name='info', description='資訊查詢：匯率、油價、CVE')

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rates: tuple[float, dict] = (0, {})
        self._oil: tuple[float, list] = (0, [])
        self._oil_history: tuple[float, tuple] = (0, ({}, None))
        self.oil_history_latest: datetime.date | None = None  # newest adjustment on the history page
        self.nvd = NVDLimiter()
        self.kev_cache: tuple[float, dict] = (0, {})

    async def cog_load(self) -> None:
        self.daily_push.start()
        self.oil_push.start()

    async def cog_unload(self) -> None:
        self.daily_push.cancel()
        self.oil_push.cancel()

    # ---- exchange rate & fuel ----

    async def rates(self) -> dict:
        fetched, data = self._rates
        if time.time() - fetched > CACHE_SECONDS:
            data = await http.get_json(RATES_URL)
            self._rates = (time.time(), data)
        return data

    async def oil_prices(self) -> list[dict]:
        fetched, rows = self._oil
        if time.time() - fetched > CACHE_SECONDS:
            root = ET.fromstring(await http.get_text(CPC_URL))
            rows = [{child.tag: child.text for child in row} for row in root.iter('tbTable')]
            self._oil = (time.time(), rows)
        return rows

    async def oil_changes(self) -> tuple[dict[str, float], datetime.date | None]:
        """Price change since the previous adjustment for each history column, and that adjustment's date.

        The history page lists every adjustment newest first; rows where a fuel's cell is empty were
        adjustments of other products only, so they are skipped per column."""
        fetched, data = self._oil_history
        if time.time() - fetched < CACHE_SECONDS:
            return data
        page = await http.get_text(CPC_HISTORY_URL)
        rows = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, flags=re.S)[:40]:  # the page holds decades of rows
            cells = [html.unescape(re.sub(r'<[^>]+>', '', c)).strip()
                     for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S)]
            if cells:
                rows.append(cells)
        header = next(r for r in rows if r and r[0] == '調價日期')
        dated = [dict(zip(header, r)) for r in rows if re.fullmatch(r'\d{4}/\d{1,2}/\d{1,2}', r[0])]
        if dated:
            self.oil_history_latest = datetime.datetime.strptime(dated[0]['調價日期'], '%Y/%m/%d').date()
        changes, previous_date = {}, None
        for column in HISTORY_COLUMN.values():
            prices = [(r['調價日期'], float(r[column])) for r in dated if r.get(column)]
            if len(prices) >= 2:
                changes[column] = round(prices[0][1] - prices[1][1], 2)
                previous_date = previous_date or datetime.datetime.strptime(prices[1][0], '%Y/%m/%d').date()
        self._oil_history = (time.time(), (changes, previous_date))
        return changes, previous_date

    @info.command(name='rate')
    @app_commands.describe(currency='外幣', amount='選填：要換算的金額')
    @app_commands.choices(currency=[app_commands.Choice(name=f'{flag} {name} {code}', value=code)
                                    for code, (flag, name) in CURRENCIES.items()])
    async def rate(self, interaction: discord.Interaction, currency: app_commands.Choice[str],
                   amount: float | None = None):
        """查詢外幣對新台幣匯率"""
        await interaction.response.defer()
        code = currency.value
        flag, name = CURRENCIES[code]
        data = await self.rates()
        per_twd = data['rates'][code]  # units of `code` per 1 TWD
        twd = 1 / per_twd
        body = f'# 1 {code} = {fmt_rate(twd)} TWD\n-# 1 TWD = {fmt_rate(per_twd)} {code}'
        if amount is not None:
            body += (f'\n\n💱 **{amount:,.2f} {code}** ≈ **{amount * twd:,.2f} TWD**\n'
                     f'💱 **{amount:,.2f} TWD** ≈ **{amount * per_twd:,.2f} {code}**')
        body += f'\n\n-# 國際參考匯率（中間價），與銀行買賣價略有差異 · 更新於 {timestamp(data["time_last_update_unix"], "R")}'
        await interaction.followup.send(embed=make_embed(f'{flag} {name}（{code}）匯率', body, theme=FINANCE))

    @info.command(name='oil')
    async def oil(self, interaction: discord.Interaction):
        """中油汽油、柴油牌價"""
        await interaction.response.defer()
        await interaction.followup.send(embed=await self.oil_embed())

    async def oil_embed(self, title: str = '⛽ 中油油價') -> discord.Embed:
        rows = [r for r in await self.oil_prices() if '公升' in (r.get('計價單位') or '')]
        try:
            changes, previous = await self.oil_changes()
        except Exception:
            log.warning('Could not read CPC price history', exc_info=True)
            changes, previous = {}, None

        def line(r: dict) -> str:
            text = f'{r["產品名稱"]}　**{float(r["參考牌價"]):.1f}** 元/公升'
            delta = changes.get(HISTORY_COLUMN.get(r['產品名稱'], ''))
            if delta is None:
                return text
            arrow = '🔺' if delta > 0 else '🔻' if delta < 0 else '➖'
            return text + f'　{arrow} {delta:+.1f}' if delta else text + f'　{arrow} 0.0'

        gasoline = [r for r in rows if '汽油' in r['產品名稱']]
        diesel = [r for r in rows if '柴油' in r['產品名稱']]
        embed = make_embed(title, theme=FINANCE, stamp=True)
        for title, group in (('汽油', gasoline), ('柴油', diesel)):
            if group:
                embed.add_field(name=title, value='\n'.join(line(r) for r in group), inline=False)
        if rows:
            effective = datetime.date.fromisoformat(rows[0]['牌價生效時間'][:10])
            note = f'{effective.month}/{effective.day} 起生效'
            if previous:
                note += f' · 漲跌與 {previous.month}/{previous.day} 相比'
            embed.description = f'-# {note} · 每週一凌晨調整'
        return embed

    # ---- weekly fuel price post ----

    @tasks.loop(minutes=30)
    @guarded
    async def oil_push(self):
        """CPC announces next week's prices on Sunday around noon (effective Monday 00:00). From OIL_TIME on
        Sunday, post once the history page lists next Monday; if it still hasn't by Monday 01:30, post anyway."""
        channel = self.bot.get_channel(config.OIL_CHANNEL_ID or 0)
        now = datetime.datetime.now(config.TZ)
        if not channel:
            return
        if now.weekday() == 6 and now.time() >= config.OIL_TIME.replace(tzinfo=None):
            monday = now.date() + datetime.timedelta(days=1)
        elif now.weekday() == 0 and now.hour < 2:
            monday = now.date()
        else:
            return
        if await db.get_kv('oil_posted') == monday.isoformat():
            return
        self._oil = self._oil_history = (0, None)  # always fetch fresh data here
        await self.oil_changes()
        latest = await self.latest_oil_date()
        if latest != monday and not (now.weekday() == 0 and now.hour * 60 + now.minute >= 90):
            return  # not announced yet: try again in half an hour
        title = '⛽ 下週油價（明天起生效）' if now.weekday() == 6 else '⛽ 本週油價'
        await channel.send(embed=await self.oil_embed(title))
        await db.set_kv('oil_posted', monday.isoformat())

    async def latest_oil_date(self) -> datetime.date | None:
        """Date of the newest prices, once both the price list and the history page (which the
        rise / fall arrows come from) have them."""
        dates = [r['牌價生效時間'][:10] for r in await self.oil_prices() if r.get('牌價生效時間')]
        if not dates or not self.oil_history_latest:
            return None
        return min(datetime.date.fromisoformat(max(dates)), self.oil_history_latest)

    # ---- CVE ----

    async def kev(self) -> dict[str, dict]:
        fetched, data = self.kev_cache
        if time.time() - fetched > 3600:
            feed = await http.get_json(KEV_FEED)
            data = {v['cveID']: v for v in feed['vulnerabilities']}
            self.kev_cache = (time.time(), data)
        return data

    async def nvd_query(self, **params) -> dict:
        await self.nvd.wait()
        headers = {'apiKey': config.NVD_API_KEY} if config.NVD_API_KEY else {}
        return await http.get_json(NVD_API, params=params, headers=headers)

    @staticmethod
    def cvss(cve: dict) -> tuple[float | None, str, str]:
        metrics = cve.get('metrics', {})
        for key in ('cvssMetricV40', 'cvssMetricV31', 'cvssMetricV30', 'cvssMetricV2'):
            if metrics.get(key):
                m = metrics[key][0]
                data = m['cvssData']
                severity = data.get('baseSeverity') or m.get('baseSeverity', '')
                return data['baseScore'], severity, data.get('vectorString', '')
        return None, '', ''

    @info.command(name='cve')
    @app_commands.describe(cve_id='例如 CVE-2021-44228')
    async def cve(self, interaction: discord.Interaction, cve_id: str):
        """在 NVD 查詢 CVE 漏洞資訊"""
        cve_id = cve_id.strip().upper()
        if not CVE_RE.fullmatch(cve_id):
            await interaction.response.send_message(embed=error_embed('格式應為 CVE-YYYY-NNNN'), ephemeral=True)
            return
        await interaction.response.defer()
        data = await self.nvd_query(cveId=cve_id)
        if not data.get('vulnerabilities'):
            await interaction.followup.send(embed=error_embed(f'NVD 上找不到 {cve_id}。'))
            return
        cve = data['vulnerabilities'][0]['cve']
        desc = next((d['value'] for d in cve.get('descriptions', []) if d['lang'] == 'en'), '（沒有描述）')
        score, severity, vector = self.cvss(cve)
        embed = make_embed(f'🛡️ {cve_id}', truncate(desc, 1500), url=f'https://nvd.nist.gov/vuln/detail/{cve_id}',
                           theme=SECURITY, color=SEVERITY_COLOR.get(severity, SECURITY.color))
        embed.add_field(name='CVSS', value=f'**{score}** {SEVERITY_LABEL.get(severity, severity)}'
                        if score is not None else '尚未評分')
        embed.add_field(name='發布日期', value=cve.get('published', '?')[:10])
        cwes = sorted({d['value'] for w in cve.get('weaknesses', []) for d in w['description']
                       if d['value'].startswith('CWE-')})
        if cwes:
            embed.add_field(name='CWE', value='\n'.join(
                f'[{c}](https://cwe.mitre.org/data/definitions/{c[4:]}.html)' for c in cwes[:5]))
        try:
            kev = (await self.kev()).get(cve_id)
        except Exception:
            kev = None
        if kev:
            embed.add_field(name='🚨 CISA KEV', value=f'已遭實際利用（{kev["dateAdded"]} 列入）', inline=False)
        if vector:
            embed.add_field(name='向量', value=f'`{vector}`', inline=False)
        refs: dict[str, str] = {}  # one link per site, in NVD's order
        for r in cve.get('references', []):
            refs.setdefault(urllib.parse.urlparse(r['url']).netloc.removeprefix('www.'), r['url'])
        if refs:
            links = ' · '.join(f'[{site}]({url})' for site, url in list(refs.items())[:8])
            embed.add_field(name='參考資料', value=truncate(links, 1024), inline=False)
        await interaction.followup.send(embed=embed)

    # ---- daily push ----

    @tasks.loop(time=config.SECURITY_TIME)
    @guarded
    async def daily_push(self):
        channel = self.bot.get_channel(config.SECURITY_CHANNEL_ID or 0)
        if not channel:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        embeds = []

        try:
            kev = await self.kev()
            recent = [v for v in kev.values()
                      if v['dateAdded'] >= (now - datetime.timedelta(days=2)).date().isoformat()]
            new_ids = await db.filter_unseen('kev', [v['cveID'] for v in recent])
            if new_ids:
                lines = [f'**[{v}](https://nvd.nist.gov/vuln/detail/{v})** · {kev[v]["vendorProject"]} '
                         f'{kev[v]["product"]}\n-# {truncate(kev[v]["shortDescription"], 180)}' for v in new_ids]
                embeds += self.list_embeds('🚨 CISA 新增：已遭實際利用的漏洞（KEV）', lines, 0x8B0000)
                await db.mark_seen('kev', new_ids, time.time())
        except Exception:
            log.warning('KEV push failed', exc_info=True)

        try:
            fmt = '%Y-%m-%dT%H:%M:%S.000Z'
            data = await self.nvd_query(pubStartDate=(now - datetime.timedelta(days=1)).strftime(fmt),
                                        pubEndDate=now.strftime(fmt), cvssV3Severity='CRITICAL',
                                        resultsPerPage=100)
            cves = {v['cve']['id']: v['cve'] for v in data.get('vulnerabilities', [])}
            new_ids = await db.filter_unseen('nvd', list(cves))
            if new_ids:
                lines = []
                for cve_id in new_ids:
                    cve = cves[cve_id]
                    score, _, _ = self.cvss(cve)
                    desc = next((d['value'] for d in cve['descriptions'] if d['lang'] == 'en'), '')
                    lines.append(f'**[{cve_id}](https://nvd.nist.gov/vuln/detail/{cve_id})** · CVSS **{score}**\n'
                                 f'-# {truncate(desc, 180)}')
                embeds += self.list_embeds('🔥 過去 24 小時新增的嚴重等級 CVE', lines, SEVERITY_COLOR['CRITICAL'])
                await db.mark_seen('nvd', new_ids, time.time())
        except Exception:
            log.warning('NVD push failed', exc_info=True)

        for embed in embeds:  # one per message: all embeds in a message share a 6000-char limit
            await channel.send(embed=embed)

    @staticmethod
    def list_embeds(title: str, lines: list[str], color: int) -> list[discord.Embed]:
        embeds, current = [], ''
        for line in lines:
            if len(current) + len(line) > 3800:
                embeds.append(make_embed(title, current, theme=SECURITY, color=color, stamp=True))
                current = ''
            current += line + '\n\n'
        if current:
            embeds.append(make_embed(title, current, theme=SECURITY, color=color, stamp=True))
        return embeds

    @daily_push.before_loop
    @oil_push.before_loop
    async def before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
