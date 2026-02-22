import math
from abc import abstractmethod

from discord import ui, ButtonStyle, Interaction
from utils.embeds import Embed

class PageView(ui.View):
    def __init__(self, page_size: int):
        super().__init__(timeout=None)
        self.page_index = 0
        self.page_size = page_size
        
    @property
    @abstractmethod
    def _title(self) -> str:
        pass
        
    @abstractmethod
    async def _load_content(self, from_idx: int, to_idx: int) -> tuple[int, str]:
        pass
    
    def position_to_page_index(self, position: int) -> int:
        return (position - 1) // self.page_size

    async def load(self) -> Embed:
        if self.page_index < 0:
            self.page_index = 0
        
        num_items, content = await self._load_content(
            (self.page_index * self.page_size),
            ((self.page_index + 1) * self.page_size - 1)
        )
        
        embed = Embed(title=self._title)
        if num_items <= 0:
            embed.set_image(url="https://c.tenor.com/1rQLxWiCtiIAAAAd/tenor.gif")
            self.clear_items() # remove buttons
            return embed

        max_page_index = self.position_to_page_index(num_items)
        if self.page_index > max_page_index:
            # if the content changed and this is out of bounds, try again
            self.page_index = max_page_index
            return await self.load()

        embed.description = content
        self.prev_page.disabled = (self.page_index <= 0)
        self.next_page.disabled = (self.page_index >= max_page_index)            
        return embed

    @ui.button(emoji="⬅", label="Prev", style=ButtonStyle.gray)
    async def prev_page(self, interaction: Interaction, _) -> None:
        self.page_index -= 1
        embed = await self.load()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(emoji="➡", label="Next", style=ButtonStyle.gray)
    async def next_page(self, interaction: Interaction, _) -> None:
        self.page_index += 1
        embed = await self.load()
        await interaction.response.edit_message(embed=embed, view=self)
        
    class JumpToModal(ui.Modal, title="Jump To Position"):
        def __init__(self, view: 'PageView'):
            super().__init__()
            self.view = view
            self.position_field = ui.TextInput(
                label="Position",
                placeholder="Enter position to jump to",
                required=True
            )
            self.add_item(self.position_field)

        async def on_submit(self, interaction: Interaction) -> None:
            position = int(self.position_field.value)
            self.view.page_index = self.view.position_to_page_index(position)
            embed = await self.view.load()
            await interaction.response.edit_message(embed=embed, view=self.view)
        
    @ui.button(label="Jump", style=ButtonStyle.gray)
    async def jump_to_position(self, interaction: Interaction, _) -> None:
        modal = self.JumpToModal(self)
        await interaction.response.send_modal(modal)
