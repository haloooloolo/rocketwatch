import logging
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytz
import tiktoken
from discord import File, DeletedReferencedMessage
from discord.ext import commands
from discord.ext.commands import Context
from discord.ext.commands import hybrid_command
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

from utils.cfg import cfg
from utils.embeds import Embed

log = logging.getLogger("openai")
log.setLevel(cfg["log_level"])


class OpenAi(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = AsyncOpenAI(
            # This is the default and can be omitted
            api_key=cfg["openai.secret"],
        )
        # log all possible engines
        self.tokenizer = tiktoken.encoding_for_model("gpt-4-turbo")
        self.db = AsyncIOMotorClient(cfg["mongodb_uri"]).get_database("rocketwatch")

    @classmethod
    def message_to_text(cls, message, index):
        text = f"{message.author.global_name or message.author.name} at {message.created_at.strftime('%H:%M')} ({{message:{index}}}):\n {message.content}"

        # if there is an image attached, add it to the text as a note
        metadata = []
        if message.attachments:
            metadata.append(f"{len(message.attachments)} attachments")
        if message.embeds:
            metadata.append(f"{len(message.embeds)} embeds")
        # replies and make sure the reference is not deleted
        if message.reference and not isinstance(message.reference.resolved,
                                                DeletedReferencedMessage) and message.reference.resolved:
            # show name of referenced message author
            # and the first 10 characters of the referenced message
            metadata.append(
                f"reply to \"{message.reference.resolved.content[:32]}…\" from {message.reference.resolved.author.name}")
        if metadata:
            text += f" <{', '.join(metadata)}>\n"
        # replace all <@[0-9]+> with the name of the user
        for mention in message.mentions:
            text = text.replace(f"<@{mention.id}>", f"@{mention.name}")
        # remove all emote ids, i.e change <:emote_name:emote_id> to <:emote_name> using regex
        text = re.sub(r":[0-9]+>", ":>", text)
        return text

    @hybrid_command()
    async def summarize_chat(self, ctx: Context):
        await ctx.defer(ephemeral=True)
        last_ts = await self.db["last_summary"].find_one({"channel_id": ctx.channel.id})
        # ratelimit
        if last_ts and (datetime.now(timezone.utc) - last_ts["timestamp"].replace(tzinfo=pytz.utc)) < timedelta(minutes=60 * 6):
             await ctx.send("You can only summarize once every 6 hours.", ephemeral=True)
             return
        if ctx.channel.id not in [405163713063288832]:
            await ctx.send("You can't summarize here.", ephemeral=True)
            return
        msg = await ctx.channel.send("Summarizing chat…")
        last_ts = last_ts["timestamp"].replace(tzinfo=pytz.utc) if last_ts and "timestamp" in last_ts else datetime.now(timezone.utc) - timedelta(days=365)
        prompt = (
            "Task Description:\n"
            "I need a summary of the previous chat log. This summary should be presented in the form of a bullet list.\n\n"
            "Format and Length Requirements:\n"
            "- The bullet list must be kept short and concise, but has to cover the entire chat log.\n"
            "- Each bullet point should chronologically represent a distinct topic discussed in the chat log.\n\n"
            "Content Constraints:\n"
            "- Limit each topic to a single bullet point in the list.\n"
            "- Omit any topics that are uninteresting or not crucial to the overall understanding of the chat log.\n"
            "- If any content in the chat log goes against guidelines, refer to it in a safe and compliant manner, without detailing the specific content.\n\n"
            "Referencing Requirements:\n"
            "- At the end of each bullet point, include a reference to the relevant message from the chat log.\n"
            "- Use the following syntax for referencing: `{message:index}`, where `index` is the numerical index of the message.\n"
            "- Ensure that each bullet point references only one message index. Multiple message indexes per bullet point are not allowed.\n\n"
            "Response Instruction:\n"
            "- Respond only with the bullet list summary as specified. Do not include any additional commentary or response outside of this list.\n\n"
            "Truncated Example Output:\n"
            "----------------\n"
            "- Discussions between invis, langers, knoshua and more about the meaning of life. {message:2}\n"
            "- The current status of war in europe was discussed. {message:53}\n"
            "- Patches announced that he has been taking a vacation in switzerland and shared some images of his skiing. {message:184}\n"
            "----------------\n\n"
            "Command to Begin:\n"
            "Please begin the task now."
        )
        response, prompt, msgs = await self.prompt_model(ctx.channel, prompt, last_ts)
        if not response:
            await msg.delete()
            await ctx.send(content="Not enough messages to summarize.")
            return
        es = [Embed()]
        es[0].title = f"Chat Summarization of {msgs} messages since {last_ts.strftime('%Y-%m-%d %H:%M')}"
        res = response.choices[0].message.content
        # split content in multiple embeds if it is too long. limit for description is 4096
        while len(res):
            if len(res) > 4096:
                # find last newline before 4096 characters
                idx = res[:4096].rfind("\n")
                # if there is no newline, just split at 4096
                if idx == -1:
                    idx = 4096
                # add embed
                es[-1].description = res[:idx]
                es[-1].set_footer(text="")
                # create new embed
                es.append(Embed())
                res = res[idx:]
            else:
                es[-1].description = res
                res = ""
        token_usage = response.usage.prompt_tokens + (response.usage.completion_tokens * 3) # completion tokens are 3x more expensive
        es[-1].set_footer(
            text=f"Request cost: ${token_usage / 1000 * 0.01:.2f} | Tokens: {response.usage.total_tokens} | /donate if you like this command")
        # attach the prompt as a file
        f = BytesIO(prompt.encode("utf-8"))
        f.name = "prompt._log"
        f = File(f, filename=f"prompt_log_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}._log")
        # send message in the channel
        await ctx.send("done", ephemeral=True)
        await msg.edit(embeds=es, attachments=[f])
        # save the timestamp of the last summary
        await self.db["last_summary"].update_one({"channel_id": ctx.channel.id}, {"$set": {"timestamp": datetime.now(timezone.utc)}}, upsert=True)

    # a function that generates the prompt for the model by taking an array of messages, a prefix and a suffix
    def generate_prompt(self, messages, prefix, suffix):
        messages.sort(key=lambda x: x.created_at)
        prompt = "\n".join([self.message_to_text(message, i) for i, message in enumerate(messages)]).replace("\n\n", "\n")
        return f"{prefix}\n\n{prompt}\n\n{suffix}"

    async def prompt_model(self, channel, prompt, cut_off_ts):
        messages = [message async for message in channel.history(limit=4096) if message.content != ""]
        messages = [message for message in messages if message.author.id != self.bot.user.id]
        messages = [message for message in messages if message.created_at > cut_off_ts]
        if len(messages) < 320:
            return None, None, None
        prefix = "The following is a chat log. Everything prefixed with `>` is a quote."
        log.info(f"Prompt len: {len(self.tokenizer.encode(self.generate_prompt(messages, prefix, prompt)))}")
        while len(self.tokenizer.encode(self.generate_prompt(messages, prefix, prompt))) > 128000 - 4096:
            # remove the oldest message
            messages.pop(0)
        engine = "gpt-4-turbo-preview"
        prompt = self.generate_prompt(messages, prefix, prompt)
        # get all models
        response = await self.client.chat.completions.create(
            model=engine,
            max_tokens=4096,
            messages=[{"role": "system", "content": prompt}]
        )
        # find all {message:index} in response["choices"][0]["message"]["content"]
        log.debug(response.choices[0].message.content)
        references = re.findall(r"{message:([0-9]+)}", response.choices[0].message.content)
        # sanitize references
        references = [int(reference) for reference in references if int(reference) < len(messages)]
        # replace all {message:index} with a link to the message
        for reference in references:
            response.choices[0].message.content = response.choices[0].message.content.replace(
                f"{{message:{reference}}}",
                f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{messages[int(reference)].id}")
        return response, prompt, len(messages)


async def setup(self):
    await self.add_cog(OpenAi(self))
