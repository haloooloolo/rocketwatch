import builtins
import logging
import re
from datetime import UTC, datetime
from operator import itemgetter
from typing import Any

from bson import CodecOptions
from discord import ButtonStyle, Interaction, Member, TextStyle, User, app_commands, ui
from discord.app_commands import Choice, Group, choices
from discord.ext.commands import Cog, GroupCog
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.config import cfg
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.file import TextFile

log = logging.getLogger("rocketwatch.support_utils")


async def generate_template_embed(
    db: AsyncDatabase[dict[str, Any]], template_name: str
) -> Embed | None:
    template = await db.support_bot.find_one({"_id": template_name})
    if not template:
        return None
    # get the last log entry from the db
    dumps_col: AsyncCollection[dict[str, Any]] = db.support_bot_dumps.with_options(
        codec_options=CodecOptions(tz_aware=True)
    )
    last_edit = await dumps_col.find_one({"template": template_name}, sort=[("ts", -1)])
    description: str = template["description"] or ""
    if last_edit and template_name != "announcement":
        description += f"\n\n*Last Edited by <@{last_edit['author']['id']}> <t:{last_edit['ts'].timestamp():.0f}:R>*"
    return Embed(title=template["title"], description=description)


# Define a simple View that gives us a counter button
class AdminView(ui.View):
    def __init__(self, db: AsyncDatabase[dict[str, Any]], template_name: str) -> None:
        super().__init__()
        self.db = db
        self.template_name = template_name

    @ui.button(label="Edit", style=ButtonStyle.blurple)
    async def edit(
        self, interaction: Interaction["RocketWatch"], _: ui.Button["AdminView"]
    ) -> None:
        template = await self.db.support_bot.find_one({"_id": self.template_name})
        if not template:
            return
        # Make sure to update the message with our update
        await interaction.response.send_modal(
            AdminModal(
                template["title"], template["description"], self.db, self.template_name
            )
        )


class DeleteMessageButton(
    ui.DynamicItem[ui.Button[Any]],
    template=r"button:delete:(?P<id>\d+)",
):
    def __init__(self, user_id: int) -> None:
        super().__init__(
            ui.Button(
                emoji="<:delete:1364953621191721002>",
                style=ButtonStyle.gray,
                custom_id=f"button:delete:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: Interaction[Any],
        item: ui.Item[Any],
        match: re.Match[str],
    ) -> "DeleteMessageButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: Interaction[Any]) -> None:
        if (interaction.user.id == self.user_id) and interaction.message:
            await interaction.message.delete()
            log.warning(
                f"Message deleted by {interaction.user} in {interaction.channel}"
            )


class DeletableView(ui.View):
    def __init__(self, user: User | Member):
        super().__init__(timeout=None)
        self.add_item(DeleteMessageButton(user.id))


class AdminModal(ui.Modal, title="Change Template Message"):
    def __init__(
        self,
        old_title: str,
        old_description: str,
        db: AsyncDatabase[dict[str, Any]],
        template_name: str,
    ) -> None:
        super().__init__()
        self.db = db
        self.old_title = old_title
        self.old_description = old_description
        self.template_name = template_name
        self.title_field: ui.TextInput[AdminModal] = ui.TextInput(
            label="Title", placeholder="Enter a title", default=old_title
        )
        self.description_field: ui.TextInput[AdminModal] = ui.TextInput(
            label="Description",
            placeholder="Enter a description",
            default=old_description,
            style=TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(self.title_field)
        self.add_item(self.description_field)

    async def on_submit(self, interaction: Interaction) -> None:
        # get the data from the db
        template = await self.db.support_bot.find_one({"_id": self.template_name})
        if not template:
            return
        # verify that no changes were made while we were editing
        if (
            template["title"] != self.old_title
            or template["description"] != self.old_description
        ):
            # dump the description into a memory file
            await interaction.response.edit_message(
                embed=Embed(
                    description=(
                        "Someone made changes while you were editing. Please try again.\n"
                        "Your pending changes have been attached to this message."
                    ),
                ),
                view=None,
            )
            a = await interaction.original_response()
            file = TextFile(
                self.description_field.value, f"{self.title_field.value}.txt"
            )
            await a.add_files(file)
            return

        try:
            await self.db.support_bot_dumps.insert_one(
                {
                    "ts": datetime.now(UTC),
                    "template": self.template_name,
                    "prev": template,
                    "new": {
                        "title": self.title_field.value,
                        "description": self.description_field.value,
                    },
                    "author": {
                        "id": interaction.user.id,
                        "name": interaction.user.name,
                    },
                }
            )
        except Exception as e:
            log.error(e)

        await self.db.support_bot.update_one(
            {"_id": self.template_name},
            {
                "$set": {
                    "title": self.title_field.value,
                    "description": self.description_field.value,
                }
            },
        )
        content = (
            f"This is a preview of the `{self.template_name}` template.\n"
            f"You can change it using the `Edit` button."
        )
        embed = await generate_template_embed(self.db, self.template_name)
        await interaction.response.edit_message(
            content=content, embed=embed, view=AdminView(self.db, self.template_name)
        )


def has_perms(interaction: Interaction) -> bool:
    user = interaction.user
    if user.id in cfg.rocketpool.support.user_ids:
        return True
    if cfg.discord.owner.user_id == user.id:
        return True
    if isinstance(user, Member):
        if any(r.id in cfg.rocketpool.support.role_ids for r in user.roles):
            return True
        if (
            user.guild_permissions.moderate_members
            and interaction.guild
            and interaction.guild.id == cfg.rocketpool.support.server_id
        ):
            return True
    return False


async def _use(
    db: AsyncDatabase[dict[str, Any]],
    interaction: Interaction,
    name: str,
    mention: User | None,
) -> None:
    # check if the template exists in the db
    template = await db.support_bot.find_one({"_id": name})
    if not template:
        await interaction.response.send_message(
            embed=Embed(
                title="Error",
                description=f"A template with the name '{name}' does not exist.",
            ),
            ephemeral=True,
        )
        return

    # respond with the template embed
    if e := (await generate_template_embed(db, name)):
        await interaction.response.send_message(
            content=mention.mention if mention else "",
            embed=e,
            view=DeletableView(interaction.user),
        )
    else:
        await interaction.response.send_message(
            embed=Embed(
                title="Error",
                description="An error occurred while generating the template embed.",
            ),
            ephemeral=True,
        )


class SupportGlobal(Cog):
    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @app_commands.command(name="use")
    async def _use(
        self, interaction: Interaction, name: str, mention: User | None
    ) -> None:
        await _use(self.bot.db, interaction, name, mention)

    @_use.autocomplete("name")
    async def match_template(
        self, interaction: Interaction, current: str
    ) -> list[Choice[str]]:
        return [
            Choice(name=c["_id"], value=c["_id"])
            for c in await self.bot.db.support_bot.find(
                {"_id": {"$regex": current, "$options": "i"}}
            ).to_list(25)
        ]


class SupportUtils(GroupCog, name="support"):
    subgroup = Group(
        name="template",
        description="various templates used by active support members",
        guild_ids=[cfg.rocketpool.support.server_id],
    )

    def __init__(self, bot: RocketWatch):
        self.bot = bot

    @subgroup.command()
    async def add(self, interaction: Interaction, name: str) -> None:
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(
                    title="Error",
                    description="You do not have permission to use this command.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template already exists in the db
        if await self.bot.db.support_bot.find_one({"_id": name}):
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' already exists.",
                ),
            )
            return
        # create the template in the db
        await self.bot.db.support_bot.insert_one(
            {
                "_id": name,
                "title": "Insert Title here",
                "description": "Insert Description here",
            }
        )
        content = (
            f"This is a preview of the `{name}` template.\n"
            f"You can change it using the `Edit` button."
        )
        embed = await generate_template_embed(self.bot.db, name)
        await interaction.edit_original_response(
            content=content, embed=embed, view=AdminView(self.bot.db, name)
        )

    @subgroup.command()
    async def edit(self, interaction: Interaction, name: str) -> None:
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(
                    title="Error",
                    description="You do not have permission to use this command.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template exists in the db
        template = await self.bot.db.support_bot.find_one({"_id": name})

        if not template:
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' does not exist.",
                ),
            )
            return

        content = (
            f"This is a preview of the `{name}` template.\n"
            f"You can change it using the `Edit` button."
        )
        embed = await generate_template_embed(self.bot.db, name)
        await interaction.edit_original_response(
            content=content, embed=embed, view=AdminView(self.bot.db, name)
        )

    @subgroup.command()
    async def remove(self, interaction: Interaction, name: str) -> None:
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(
                    title="Error",
                    description="You do not have permission to use this command.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template exists in the db
        template = await self.bot.db.support_bot.find_one({"_id": name})
        if not template:
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' does not exist.",
                ),
            )
            return
        # remove the template from the db
        await self.bot.db.support_bot.delete_one({"_id": name})
        await interaction.edit_original_response(
            embed=Embed(title="Success", description=f"Template '{name}' removed."),
        )

    @subgroup.command()
    @choices(
        order_by=[
            Choice(name="Name", value="_id"),
            Choice(name="Last Edited Date", value="last_edited_date"),
        ]
    )
    async def list(self, interaction: Interaction, order_by: str = "_id") -> None:
        await interaction.response.defer(ephemeral=True)
        # get all templates and their last edited date using the support_bot_dumps collection
        templates = await (
            await self.bot.db.support_bot.aggregate(
                [
                    {
                        "$lookup": {
                            "from": "support_bot_dumps",
                            "localField": "_id",
                            "foreignField": "template",
                            "as": "dump",
                        }
                    },
                    {
                        "$project": {
                            "_id": 1,
                            "last_edited_date": {"$arrayElemAt": ["$dump.ts", 0]},
                        }
                    },
                ]
            )
        ).to_list()
        # sort the templates by the specified order
        templates.sort(key=itemgetter(order_by))
        # create the embed
        embed = Embed(title="Templates")
        embed.description = (
            "".join(
                f"\n`{template['_id']}` - <t:{template.get('last_edited_date', datetime.now()).timestamp():.0f}:R>"
                for template in templates
            )
            + ""
        )
        # split the embed into multiple embeds if it is too long
        embeds = [embed]
        while len(embeds[-1]) > 6000:
            embeds.append(Embed())
            embeds[-1].title = embed.title
            embeds[-1].description = embed.description[6000:]
            embed.description = embed.description[:6000]
        await interaction.edit_original_response(embeds=embeds)

    @subgroup.command()
    async def use(
        self, interaction: Interaction, name: str, mention: User | None
    ) -> None:
        await _use(self.bot.db, interaction, name, mention)

    @edit.autocomplete("name")
    @remove.autocomplete("name")
    @use.autocomplete("name")
    async def match_template(
        self, interaction: Interaction, current: str
    ) -> builtins.list[Choice[str]]:
        return [
            Choice(name=c["_id"], value=c["_id"])
            for c in await self.bot.db.support_bot.find(
                {"_id": {"$regex": current, "$options": "i"}}
            ).to_list(25)
        ]


async def setup(self: RocketWatch) -> None:
    self.add_dynamic_items(DeleteMessageButton)
    await self.add_cog(SupportUtils(self))
    await self.add_cog(SupportGlobal(self))
