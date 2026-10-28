import discord

Page = discord.Embed | list[discord.Embed]


class Paginator(discord.ui.View):
    """◀ / ▶ buttons over pages. A page is one embed, or a list of up to 10 embeds shown together.
    Only the user who ran the command can flip pages."""

    def __init__(self, pages: list[Page], owner_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.pages = [page if isinstance(page, list) else [page] for page in pages]
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.Message | None = None
        for i, page in enumerate(self.pages):
            last = page[-1]  # the page number goes on the bottom embed
            footer = last.footer.text or ''
            last.set_footer(text=f'第 {i + 1}/{len(self.pages)} 頁 · {footer}'.rstrip(' ·'),
                            icon_url=last.footer.icon_url)
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('只有執行指令的人可以翻頁喔。', ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji='◀', style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embeds=self.pages[self.index], view=self)

    @discord.ui.button(emoji='▶', style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embeds=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def send_pages(interaction: discord.Interaction, pages: list[Page], ephemeral: bool = False) -> None:
    """Send pages as one paginated message (works whether or not the interaction was deferred)."""
    send = interaction.followup.send if interaction.response.is_done() else None
    if len(pages) == 1:
        first = pages[0] if isinstance(pages[0], list) else [pages[0]]
        if send:
            await send(embeds=first, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embeds=first, ephemeral=ephemeral)
        return
    view = Paginator(pages, interaction.user.id)
    first = view.pages[0]
    if send:
        view.message = await send(embeds=first, view=view, ephemeral=ephemeral, wait=True)
    else:
        await interaction.response.send_message(embeds=first, view=view, ephemeral=ephemeral)
        view.message = await interaction.original_response()
