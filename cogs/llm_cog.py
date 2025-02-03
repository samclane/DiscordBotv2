import random
import time
import discord
from discord import app_commands
from discord.ext import commands
import os
import google.generativeai as genai
import json

prompt_template = (
    "You are a Discord bot who occasionally chimes in with insights and comments related to the conversation. "
    "Your persona should be like a gamer who can be sarcastic, funny, and a bit of a troll. "
    "You sometimes ask questions or prod the users. "
    "Your name is {name}. "
    "Don't include your name in your response; Discord will handle that. "
    "Also don't include the messages or this prompt in your response. "
    "Weight the most recent messages more heavily. "
    "Here are the last {number} messages in chat:\n"
    "{messages}\n"
    "Put your response here: "
)

ner_template = (
    "You are an extremely simple Named Entity Recognition model. "
    "Your only job is to determine whether the most recent message is talking about the discord bot or not. "
    "Here are the last {number} messages in chat:\n"
    "{messages}\n"
    "Put your response here: "
)


RESPONSE_CHANCE = 0.1
MESSAGE_LIMIT = 10
MODEL = "models/gemini-2.0-flash-exp"
MESSAGE_RECENCY_SECONDS = 30


@app_commands.guild_only()
class LLMCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        self.model_name = MODEL
        self.model = genai.GenerativeModel(self.model_name)
        self.recent_messages_timestamp = {}

    @property
    def max_tokens(self):
        return genai.get_model(self.model_name).input_token_limit

    def messages_to_string(self, messages: list[discord.Message]) -> str:
        return "\n".join(
            f"{msg.author.display_name}: {msg.content}" for msg in reversed(messages)
        )

    def is_about_bot(self, messages: list[discord.Message]):
        bot_response = self.model.generate_content(
            ner_template.format(
                messages=self.messages_to_string(messages), number=len(messages)
            ),
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json", response_schema=bool
            ),
        )
        try:
            json_response = json.loads(bot_response.text)
            return bool(json_response)
        except json.JSONDecodeError:
            return False

    async def recent_messages(self, message: discord.Message) -> list[discord.Message]:
        # Gather context from channel history
        filled_messages = []
        current_tokens = 0

        async for msg in message.channel.history(
            limit=MESSAGE_LIMIT, oldest_first=False
        ):
            tokens_count = (
                self.model.count_tokens(msg.content).total_tokens if msg.content else 0
            )
            if current_tokens + tokens_count > self.max_tokens:
                break
            filled_messages.append(msg)
            current_tokens += tokens_count

        return filled_messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages from the bot itself
        if message.author == self.bot.user:
            return

        now = time.time()
        active = (
            now - self.recent_messages_timestamp.get(message.channel.id, 0)
        ) < MESSAGE_RECENCY_SECONDS

        filled_messages = await self.recent_messages(message)

        # Decide whether to respond:
        if self.bot.user.mentioned_in(message) or active:
            should_respond = True
        else:
            if self.is_about_bot(filled_messages[-1:]):
                should_respond = True
            else:
                should_respond = random.random() < RESPONSE_CHANCE

        if should_respond:
            await self.generate_response(message, now, filled_messages)

    async def generate_response(
        self,
        incoming_message: discord.Message,
        current_timestamp: float,
        message_history: list[discord.Message],
    ):
        async with incoming_message.channel.typing():
            prompt = prompt_template.format(
                messages=self.messages_to_string(message_history),
                name=self.bot.user.name,
                number=len(message_history),
            )
            response = self.model.generate_content(prompt).text
            await incoming_message.channel.send(response)
            self.recent_messages_timestamp[
                incoming_message.channel.id
            ] = current_timestamp
