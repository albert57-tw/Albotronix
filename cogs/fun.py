import random
import re

import discord
from discord import app_commands
from discord.ext import commands

import config
from core import http
from core.embeds import error_embed, make_embed


class Fun(commands.Cog):
    fun = app_commands.Group(name='fun', description='小工具：抽籤、機率、名言、測試連線')

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @fun.command(name='ping')
    async def ping(self, interaction: discord.Interaction):
        """測試 bot 是否在線"""
        await interaction.response.send_message(f'🏓 Pong！延遲 `{self.bot.latency * 1000:.0f} ms`')

    @fun.command(name='inspire')
    async def inspire(self, interaction: discord.Interaction):
        """來一句激勵人心的名言 ✨"""
        quote = (await http.get_json('https://zenquotes.io/api/random'))[0]
        embed = make_embed(description=f'### “{quote["q"]}”\n— *{quote["a"]}*')
        await interaction.response.send_message(embed=embed)

    @fun.command(name='choose')
    @app_commands.describe(options='用空白、逗號或 | 分隔選項')
    async def choose(self, interaction: discord.Interaction, options: str):
        """幫你從幾個選項中挑一個 🎲"""
        sep = r'[|,，、]' if re.search(r'[|,，、]', options) else r'\s+'
        items = [o.strip() for o in re.split(sep, options) if o.strip()]
        if not items:
            await interaction.response.send_message(embed=error_embed('至少給我一個選項吧。'), ephemeral=True)
            return
        embed = make_embed(description=f'🎲 我選……\n# {random.choice(items)}\n-# 選項：{"、".join(items)[:300]}')
        await interaction.response.send_message(embed=embed)

    @fun.command(name='chance')
    @app_commands.describe(question='想問什麼的機率？')
    async def chance(self, interaction: discord.Interaction, question: str | None = None):
        """🔮 算算看機率有多高"""
        rnd = random.random()
        if rnd > 0.75:
            comment = '機率很高！🔥'
        elif rnd < 0.25:
            comment = '有點低……💦'
        else:
            comment = '一半一半 🤔'
        embed = make_embed(description=(f'🔮 **{question}**\n' if question else '🔮 ')
                           + f'# {rnd * 100:.1f}%\n{comment}')
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if config.REACT_USER_ID and message.author.id == config.REACT_USER_ID:
            try:
                await message.add_reaction(config.REACT_EMOJI)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
