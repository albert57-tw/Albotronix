"""Opt-in notification roles: members pick which "starts in 1 hour" reminders should ping them.

Only those reminders ping; every other scheduled post is a plain message. Each topic maps to a role
(created on first use and remembered in the kv table; CTF reuses CTF_PING_ROLE_ID when set), and
events.py calls ping_kwargs(bot, topic) to mention only that role.
"""
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import config
from core import db
from core.embeds import GENERAL as THEME, error_embed, make_embed, note_embed

log = logging.getLogger(__name__)

# topic -> (emoji, role / button name, what it pings for)
TOPICS = {
    'ctf': ('🚩', 'CTF 通知', 'CTF 開賽前 1 小時提醒'),
    'contest': ('🧮', '競程通知', 'Codeforces／AtCoder 開賽前 1 小時提醒'),
}
NO_PERMISSION = 'bot 需要「管理身分組」權限，而且 bot 的身分組要排在這些通知身分組上面。'


def guild_of(bot: commands.Bot) -> discord.Guild | None:
    return bot.get_guild(config.GUILD_ID)


async def get_role(guild: discord.Guild, topic: str, create: bool = False) -> discord.Role | None:
    role_id = config.CTF_PING_ROLE_ID if topic == 'ctf' and config.CTF_PING_ROLE_ID else None
    if role_id is None:
        stored = await db.get_kv(f'sub_role:{topic}')
        role_id = int(stored) if stored else None
    role = guild.get_role(role_id) if role_id else None
    if role is None and create:
        emoji, name, _ = TOPICS[topic]
        role = await guild.create_role(name=name, mentionable=False, reason='Albotronix 訂閱通知')
        await db.set_kv(f'sub_role:{topic}', str(role.id))
    return role


async def ping_kwargs(bot: commands.Bot, topic: str) -> dict:
    """Keyword arguments for channel.send(): mention the topic's role if anyone can subscribe to it."""
    guild = guild_of(bot)
    role = await get_role(guild, topic) if guild else None
    if role is None:  # nobody has ever subscribed
        return {'content': None, 'allowed_mentions': discord.AllowedMentions.none()}
    return {'content': role.mention, 'allowed_mentions': discord.AllowedMentions(roles=[role], everyone=False, users=False)}


async def toggle(member: discord.Member, topic: str, want: bool | None = None) -> bool:
    """Add/remove the topic role. want=None flips it. Returns whether the member is now subscribed."""
    role = await get_role(member.guild, topic, create=True)
    subscribed = role in member.roles
    want = not subscribed if want is None else want
    if want and not subscribed:
        await member.add_roles(role, reason='訂閱通知')
    elif not want and subscribed:
        await member.remove_roles(role, reason='取消訂閱通知')
    return want


async def status_lines(member: discord.Member) -> list[str]:
    lines = []
    for topic, (emoji, name, what) in TOPICS.items():
        role = await get_role(member.guild, topic)
        mark = '✅' if role and role in member.roles else '⬜'
        lines.append(f'{mark} {emoji} **{name}**\n-# {what}')
    return lines


class TopicSelect(discord.ui.Select):
    def __init__(self, current: set[str]):
        options = [discord.SelectOption(label=name, value=topic, emoji=emoji, description=what[:100],
                                        default=topic in current)
                   for topic, (emoji, name, what) in TOPICS.items()]
        super().__init__(placeholder='勾選想被 tag 的通知（可複選）', min_values=0, max_values=len(options),
                         options=options)

    async def callback(self, interaction: discord.Interaction):
        chosen = set(self.values)
        try:
            for topic in TOPICS:
                await toggle(interaction.user, topic, topic in chosen)
        except discord.Forbidden:
            await interaction.response.edit_message(embed=error_embed(NO_PERMISSION), view=None)
            return
        lines = await status_lines(interaction.user)
        await interaction.response.edit_message(
            embed=make_embed('🔔 已更新你的通知訂閱', '\n'.join(lines), theme=THEME), view=None)


class SubscribeButton(discord.ui.DynamicItem[discord.ui.Button], template=r'sub:(?P<topic>[a-z]+)'):
    """Persistent button on the /notify panel message (keeps working after restarts)."""

    def __init__(self, topic: str):
        emoji, name, _ = TOPICS.get(topic, ('❔', topic, ''))
        super().__init__(discord.ui.Button(label=name, emoji=emoji, style=discord.ButtonStyle.secondary,
                                           custom_id=f'sub:{topic}'))
        self.topic = topic

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match['topic'])

    async def callback(self, interaction: discord.Interaction):
        if self.topic not in TOPICS:
            await interaction.response.send_message('這個通知已經不存在了。', ephemeral=True)
            return
        try:
            now_on = await toggle(interaction.user, self.topic)
        except discord.Forbidden:
            await interaction.response.send_message(embed=error_embed(NO_PERMISSION), ephemeral=True)
            return
        emoji, name, _ = TOPICS[self.topic]
        text = f'✅ 已訂閱 {emoji} **{name}**，開賽前 1 小時會 tag 你。' if now_on else f'🔕 已取消訂閱 {emoji} **{name}**。'
        await interaction.response.send_message(embed=note_embed(text, THEME), ephemeral=True)


class Subscriptions(commands.Cog):
    notify = app_commands.Group(name='notify', description='通知訂閱：選擇哪些開賽提醒要 tag 你')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(SubscribeButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(SubscribeButton)

    @notify.command(name='settings')
    async def subscribe(self, interaction: discord.Interaction):
        """選擇哪些開賽提醒要 tag 你"""
        current = set()
        for topic in TOPICS:
            role = await get_role(interaction.guild, topic)
            if role and role in interaction.user.roles:
                current.add(topic)
        view = discord.ui.View(timeout=300)
        view.add_item(TopicSelect(current))
        lines = await status_lines(interaction.user)
        embed = make_embed('🔔 通知訂閱', '\n'.join(lines) + '\n\n-# 其他推播（天氣、油價、每日一題、通阻等）都直接發在頻道，不會 tag 人', theme=THEME)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @notify.command(name='panel')
    async def subscribe_panel(self, interaction: discord.Interaction):
        """在這個頻道貼出訂閱按鈕面板（需要「管理身分組」權限）"""
        if not interaction.permissions.manage_roles:
            await interaction.response.send_message(embed=error_embed('你需要「管理身分組」權限。'), ephemeral=True)
            return
        view = discord.ui.View(timeout=None)
        for topic in TOPICS:
            view.add_item(SubscribeButton(topic))
        lines = [f'{emoji} **{name}** — {what}' for emoji, name, what in TOPICS.values()]
        embed = make_embed('🔔 訂閱通知', '按下按鈕加入或取消，比賽開始前 1 小時 bot 會 tag 你。\n\n' + '\n'.join(lines),
                           theme=THEME)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Subscriptions(bot))
