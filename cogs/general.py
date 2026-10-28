import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import error_embed, make_embed

# Cog class name -> (emoji, category name, one-line summary), in display order. /help itself isn't listed.
CATEGORIES = {
    'THSR': ('🚄', '高鐵', '票價、時刻與座位、停靠站、即時看板、營運狀況'),
    'TRA': ('🚆', '台鐵', '時刻表、車次、即時看板、票價、營運狀況'),
    'Metro': ('🚇', '捷運', '即時到站、票價與轉乘、首末班車、營運狀況'),
    'Bus': ('🚌', '公車', '站牌即時到站（30 分鐘內）'),
    'Weather': ('🌤️', '天氣地震', '36 小時天氣預報、最新地震報告'),
    'CTF': ('🚩', 'CTF', '近期賽程、隊伍排名、比賽頻道與題目討論串'),
    'Contests': ('🧮', '競程', 'Codeforces／AtCoder 賽程、LeetCode 與 CSES 每日一題'),
    'Info': ('📚', '資訊查詢', '匯率、油價、CVE 漏洞'),
    'Reminder': ('⏰', '提醒', '設定提醒、倒數計時'),
    'Vote': ('📊', '投票', '按鈕投票'),
    'AI': ('✨', 'AI', 'Gemini 問答、對話摘要'),
    'Fun': ('🎲', '小工具', '抽籤、機率、名言、測試連線'),
    'Subscriptions': ('🔔', '通知訂閱', '選擇哪些開賽提醒要 tag 你'),
}
OVERVIEW = 'overview'


def _walk(commands_: list) -> list[app_commands.Command]:
    """Flatten groups into their subcommands."""
    result = []
    for cmd in commands_:
        if isinstance(cmd, app_commands.Group):
            result.extend(_walk(cmd.commands))
        else:
            result.append(cmd)
    return result


def _cog_of(command: app_commands.Command | app_commands.Group) -> str:
    """Class name of the cog a command (or a group's first subcommand) belongs to."""
    if isinstance(command, app_commands.Group):
        command = next(iter(_walk([command])), None)
    binding = getattr(command, 'binding', None)
    return type(binding).__name__ if binding else 'General'


def _params(cmd: app_commands.Command) -> str:
    if not cmd.parameters:
        return ''
    parts = [f'`{p.display_name}`{"" if p.required else "（選填）"} {p.description or ""}'.strip() for p in cmd.parameters]
    return '\n-# ' + '\n-# '.join(parts)


class HelpView(discord.ui.View):
    def __init__(self, cog: 'General'):
        super().__init__(timeout=300)
        self.cog = cog
        self.message: discord.Message | None = None
        self.add_item(CategorySelect(cog))

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class CategorySelect(discord.ui.Select):
    def __init__(self, cog: 'General'):
        self.cog = cog
        options = [discord.SelectOption(label='總覽', value=OVERVIEW, emoji='📖', description='所有指令一覽')]
        options += [discord.SelectOption(label=name, value=key, emoji=emoji, description=summary[:100])
                    for key, (emoji, name, summary) in CATEGORIES.items() if key in cog.categories()]
        super().__init__(placeholder='選擇類別，查看指令說明…', options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        embed = self.cog.overview_embed() if key == OVERVIEW else self.cog.category_embed(key)
        await interaction.response.edit_message(embed=embed, view=self.view)


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _all_commands(self) -> list[app_commands.Command]:
        return sorted(_walk(self.bot.tree.get_commands()), key=lambda c: c.qualified_name)

    def categories(self) -> dict[str, list]:
        """Cog name -> its top-level commands/groups, for every category that has commands."""
        found: dict[str, list] = {}
        for top in self.bot.tree.get_commands():
            cog = _cog_of(top)
            if cog in CATEGORIES:
                found.setdefault(cog, []).append(top)
        return {key: found[key] for key in CATEGORIES if key in found}

    def overview_embed(self) -> discord.Embed:
        cats = self.categories()
        total = len([c for c in self._all_commands() if _cog_of(c) in CATEGORIES])
        embed = make_embed('Albotronix 使用說明',
                           f'從下方選單選擇類別，查看每個指令的用法。\n-# {len(cats)} 類 · {total} 個指令 · 說明只有你看得到',
                           thumbnail=self.bot.user.display_avatar.url)
        for key, tops in cats.items():
            emoji, name, _ = CATEGORIES[key]
            lines = []
            for top in tops:
                if isinstance(top, app_commands.Group):
                    lines.append(f'`/{top.name}`　' + ' · '.join(c.name for c in top.commands))
                else:
                    lines.append(f'`/{top.name}`')
            embed.add_field(name=f'{emoji} {name}', value='\n'.join(lines), inline=False)
        return embed

    def category_embed(self, key: str) -> discord.Embed:
        emoji, name, summary = CATEGORIES[key]
        embed = make_embed(f'{emoji} {name}', f'-# {summary}')
        for top in self.categories().get(key, []):
            for cmd in _walk([top]):
                embed.add_field(name=f'/{cmd.qualified_name}', value=f'{cmd.description}{_params(cmd)}'[:1024],
                                inline=False)
        return embed

    async def _command_autocomplete(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=f'/{c.qualified_name} — {c.description}'[:100], value=c.qualified_name)
                for c in self._all_commands() if current.lower() in c.qualified_name][:25]

    @app_commands.command()
    @app_commands.describe(command='直接查看某個指令的用法（選填）')
    @app_commands.autocomplete(command=_command_autocomplete)
    async def help(self, interaction: discord.Interaction, command: str | None = None):
        """我能做什麼？"""
        if command:
            cmd = next((c for c in self._all_commands() if c.qualified_name == command), None)
            if not cmd:
                await interaction.response.send_message(embed=error_embed('找不到這個指令。'), ephemeral=True)
                return
            embed = make_embed(f'/{cmd.qualified_name}', f'{cmd.description}{_params(cmd)}')
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        view = HelpView(self)
        await interaction.response.send_message(embed=self.overview_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
