from unittest.mock import AsyncMock, MagicMock

from rocketwatch.utils.views import PageView


class _ScriptedPageView(PageView):
    """Test subclass: feeds the abstract API from a Python list."""

    def __init__(self, items: list[str], page_size: int = 10) -> None:
        super().__init__(page_size=page_size)
        self._items = items

    @property
    def _title(self) -> str:
        return "Test View"

    async def _load_content(self, from_idx: int, to_idx: int) -> tuple[int, str]:
        # The view passes inclusive 0-indexed bounds; the contract is that
        # the implementation returns (total_count, page_text).
        page = self._items[from_idx : to_idx + 1]
        return len(self._items), "\n".join(page)


class TestPositionToPageIndex:
    def test_first_page_when_position_within_page_size(self) -> None:
        v = _ScriptedPageView(items=[], page_size=10)
        assert v.position_to_page_index(1) == 0
        assert v.position_to_page_index(10) == 0
        assert v.position_to_page_index(11) == 1


class TestLoad:
    async def test_empty_collection_returns_fallback_image(self) -> None:
        v = _ScriptedPageView(items=[], page_size=5)
        embed = await v.load()
        # Empty → image fallback + buttons cleared.
        assert embed.image is not None
        assert embed.image.url is not None
        assert v.children == []

    async def test_first_page_renders_first_page_items(self) -> None:
        items = [f"item-{i}" for i in range(25)]
        v = _ScriptedPageView(items=items, page_size=10)
        embed = await v.load()
        assert embed.description == "\n".join(items[:10])
        # Prev disabled on page 0; next enabled because more pages exist.
        assert v.prev_page.disabled is True
        assert v.next_page.disabled is False

    async def test_last_page_disables_next_button(self) -> None:
        items = [f"item-{i}" for i in range(25)]
        v = _ScriptedPageView(items=items, page_size=10)
        v.page_index = 2  # last page (item-20..item-24)
        embed = await v.load()
        assert embed.description == "\n".join(items[20:25])
        assert v.prev_page.disabled is False
        assert v.next_page.disabled is True

    async def test_negative_index_clamps_to_zero(self) -> None:
        items = [f"item-{i}" for i in range(5)]
        v = _ScriptedPageView(items=items, page_size=10)
        v.page_index = -3
        await v.load()
        assert v.page_index == 0

    async def test_out_of_range_index_falls_back_to_last_page(self) -> None:
        items = [f"item-{i}" for i in range(25)]
        v = _ScriptedPageView(items=items, page_size=10)
        v.page_index = 99
        await v.load()
        # 25 items / page_size 10 → max page index = (25-1)//10 = 2.
        assert v.page_index == 2


class TestJumpToModal:
    async def test_submit_routes_position_through_page_index(self) -> None:
        v = _ScriptedPageView(items=[f"i-{i}" for i in range(30)], page_size=10)
        modal = PageView.JumpToModal(v)
        # The real TextInput uses a read-only `value` property; swap the field
        # for a MagicMock so on_submit reads our synthetic value.
        modal.position_field = MagicMock()
        modal.position_field.value = "15"

        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        await modal.on_submit(interaction)
        # Position 15 with page_size 10 → page index 1.
        assert v.page_index == 1
        interaction.response.edit_message.assert_awaited_once()
