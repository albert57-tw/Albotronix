"""Paged lists of Components V2 cards (one full-width container per item), used by /ctf and /contest upcoming."""
import discord

import config


class CardList(discord.ui.LayoutView):
    """`per_page` cards per page. With owner_id it gets ◀ ▶ buttons only that user can press."""

    def __init__(self, cards: list[discord.ui.Container], title: str, label: str, per_page: int, page: int = 0,
                 owner_id: int | None = None):
        super().__init__(timeout=300 if owner_id else None)
        self.pages = [cards[i:i + per_page] for i in range(0, len(cards), per_page)]
        self.title, self.label, self.index, self.owner_id = title, label, page, owner_id
        self.message: discord.Message | None = None
        self.render(buttons=owner_id is not None)

    def render(self, buttons: bool) -> None:
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(f'## {self.title}'))
        for card in self.pages[self.index]:
            self.add_item(card)
        footer = f'{self.label} · {config.BOT_NAME} © {config.FOOTER_TEXT}'
        if len(self.pages) > 1:
            footer = f'第 {self.index + 1}/{len(self.pages)} 頁 · {footer}'
        self.add_item(discord.ui.TextDisplay(f'-# {footer}'))
        if buttons and len(self.pages) > 1:
            prev, nxt = PageButton(-1, '◀'), PageButton(1, '▶')
            prev.disabled = self.index == 0
            nxt.disabled = self.index == len(self.pages) - 1
            self.add_item(discord.ui.ActionRow(prev, nxt))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('只有執行指令的人可以翻頁喔。', ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message:
            self.render(buttons=False)
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PageButton(discord.ui.Button):
    def __init__(self, step: int, emoji: str):
        super().__init__(emoji=emoji, style=discord.ButtonStyle.secondary)
        self.step = step

    async def callback(self, interaction: discord.Interaction):
        view: CardList = self.view
        view.index += self.step
        view.render(buttons=True)
        await interaction.response.edit_message(view=view)


def card(text: str, thumbnail: str | None, colour: int) -> discord.ui.Container:
    """Containers are always full width (embeds size to their content), so every card lines up."""
    body = discord.ui.TextDisplay(text)
    if thumbnail:
        body = discord.ui.Section(body, accessory=discord.ui.Thumbnail(thumbnail))
    return discord.ui.Container(body, accent_colour=colour)
