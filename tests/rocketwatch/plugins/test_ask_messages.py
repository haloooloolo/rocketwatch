from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rocketwatch.plugins.ask import messages
from rocketwatch.plugins.ask.messages import MediaBudget, read_message
from rocketwatch.utils.llm import ImageInput


def _message(content: str = "", **fields: Any) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.content = content
    message.clean_content = content
    message.embeds = []
    message.components = []
    message.attachments = []
    message.stickers = []
    message.poll = None
    message.reactions = []
    message.message_snapshots = []
    for name, value in fields.items():
        setattr(message, name, value)
    return message


def _attachment(
    filename: str, content_type: str, data: bytes = b"data", **fields: Any
) -> MagicMock:
    att = MagicMock(spec=discord.Attachment)
    att.filename = filename
    att.content_type = content_type
    att.size = len(data)
    att.description = None
    att.duration = None
    att.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}"
    att.proxy_url = f"https://media.discordapp.net/attachments/1/2/{filename}"
    att.is_spoiler = MagicMock(return_value=False)
    att.is_voice_message = MagicMock(return_value=False)
    att.read = AsyncMock(return_value=data)
    for name, value in fields.items():
        setattr(att, name, value)
    return att


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    download = AsyncMock(return_value=ImageInput(b"img", "image/png"))
    monkeypatch.setattr(messages, "_download_image", download)
    return download


def _budget() -> MediaBudget:
    return MediaBudget(images=4, documents=1)


async def test_text_resolves_mentions() -> None:
    message = _message("<@123> look", clean_content="@alice look")
    assert (await read_message(message, _budget())).text == "@alice look"


async def test_every_embed_part(downloads: AsyncMock) -> None:
    embed = discord.Embed(
        title="Proposal 12",
        url="https://dao.example/12",
        description="Raise the fee",
        timestamp=datetime(2026, 10, 24, tzinfo=UTC),
    )
    embed.set_author(name="Data from rocketdash", url="https://rocketdash.net")
    embed.add_field(name="Votes", value="61% for")
    embed.set_footer(text="Ends soon")
    embed.set_image(url="https://cdn.example/chart.png")

    content = await read_message(_message(embeds=[embed]), _budget())

    lines = content.text.splitlines()
    assert lines[:5] == [
        "Data from rocketdash <https://rocketdash.net>",
        "**Proposal 12** <https://dao.example/12>",
        "Raise the fee",
        "Votes: 61% for",
        "Footer: Ends soon",
    ]
    assert "Time: <t:1792800000>" in lines
    downloads.assert_awaited_once_with("https://cdn.example/chart.png")
    assert content.images == [ImageInput(b"img", "image/png")]


async def test_embed_copy_of_an_attachment_is_read_once(downloads: AsyncMock) -> None:
    chart = _attachment("chart.png", "image/png")
    embed = discord.Embed(description="event")
    # Discord points the embed at the uploaded file once the message is sent
    embed.set_image(url=f"{chart.url}?ex=abc")

    content = await read_message(
        _message(embeds=[embed], attachments=[chart]), _budget()
    )

    assert len(content.images) == 1
    downloads.assert_not_awaited()


@pytest.mark.parametrize(
    ("att", "line"),
    [
        (_attachment("notes.csv", "text/csv", b"a,b"), "[Attachment notes.csv]\na,b"),
        (
            _attachment(
                "voice.ogg",
                "audio/ogg",
                duration=12.4,
                is_voice_message=MagicMock(return_value=True),
            ),
            "[Voice message, 12 s]",
        ),
        (
            _attachment("clip.mp4", "video/mp4", b"x" * 2048),
            "[File clip.mp4 (video/mp4, 2 KB)]",
        ),
        (
            _attachment(
                "secret.png", "image/png", is_spoiler=MagicMock(return_value=True)
            ),
            "[Image secret.png (spoiler)]",
        ),
    ],
)
async def test_attachments_are_described(att: MagicMock, line: str) -> None:
    assert line in (await read_message(_message(attachments=[att]), _budget())).text


async def test_pdfs_and_images_go_to_the_model_within_budget() -> None:
    files = [
        _attachment("audit.pdf", "application/pdf", b"%PDF"),
        _attachment("extra.pdf", "application/pdf", b"%PDF"),
        _attachment("a.png", "image/png"),
        _attachment("b.png", "image/png"),
    ]
    budget = MediaBudget(images=1, documents=1)

    content = await read_message(_message(attachments=files), budget)

    assert [d.data for d in content.documents] == [b"%PDF"]
    assert len(content.images) == 1
    assert "[PDF extra.pdf, 0 KB, not shown]" in content.text
    assert "[Image b.png, not shown]" in content.text
    assert budget == MediaBudget(images=0, documents=0)


async def test_components(downloads: AsyncMock) -> None:
    components = [
        discord.components._component_factory(c)
        for c in [
            {"type": 10, "content": "card text"},
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 5, "label": "Open", "url": "https://x.y"}
                ],
            },
            {"type": 12, "items": [{"media": {"url": "https://cdn.example/g.png"}}]},
        ]
    ]

    content = await read_message(_message(components=components), _budget())

    assert "card text" in content.text
    assert "[Button Open → https://x.y]" in content.text
    downloads.assert_awaited_once_with("https://cdn.example/g.png")


async def test_poll_stickers_and_reactions() -> None:
    poll = MagicMock()
    poll.question = "Raise the fee?"
    poll.answers = [
        MagicMock(text="Yes", vote_count=7),
        MagicMock(text="No", vote_count=2),
    ]
    poll.is_finalised.return_value = True
    sticker = MagicMock()
    sticker.name = "rocket"
    reaction = MagicMock(emoji="🚀", count=3)

    content = await read_message(
        _message(poll=poll, stickers=[sticker], reactions=[reaction]), _budget()
    )

    assert "[Poll, final: Raise the fee?: Yes (7), No (2)]" in content.text
    assert "[Sticker rocket]" in content.text
    assert "Reactions: 🚀 3" in content.text


async def test_forwarded_message_shares_the_budget() -> None:
    snapshot = MagicMock(spec=discord.MessageSnapshot)
    snapshot.content = "original post"
    snapshot.embeds = []
    snapshot.components = []
    snapshot.stickers = []
    snapshot.attachments = [_attachment("shot.png", "image/png")]
    budget = MediaBudget(images=1, documents=0)

    content = await read_message(_message("fyi", message_snapshots=[snapshot]), budget)

    assert "[Forwarded message]\noriginal post" in content.text
    assert len(content.images) == 1
    assert budget.images == 0
