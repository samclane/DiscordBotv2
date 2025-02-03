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
    "Here are the last {number} messages in chat:\n"
    "{messages}\n"
    "Put your response here: "
)

ner_template = (
    "You are an extremely simple Named Entity Recognition model. "
    "Your only job is to determine wheter the message is talking about the discord bot or not. "
    "Here's the message: \n"
    "{message}\n"
    "Put your response here: "
)


RESPONSE_CHANCE = 0.1
MESSAGE_LIMIT = 100
MODEL = "models/gemini-2.0-flash-exp"
MESSAGE_RECENCY_SECONDS = 300

@app_commands.guild_only()
class LLMCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        self.model_name = MODEL
        self.model = genai.GenerativeModel(self.model_name)
        self.recent_chats = {}

    def is_about_bot(self, message: str):
        bot_response = self.model.generate_content(
            ner_template.format(message=message),
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json", response_schema=bool))
        try:
            json_response = json.loads(bot_response.text)
            return bool(json_response)
        except json.JSONDecodeError:
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages from the bot itself
        if message.author == self.bot.user:
            return

        now = time.time()
        active = (now - self.recent_chats.get(message.channel.id, 0)) < MESSAGE_RECENCY_SECONDS

        # Decide whether to respond:
        if self.bot.user.mentioned_in(message) or active:
            should_respond = True
        else:
            if self.is_about_bot(message.content):
                should_respond = True
            else:
                should_respond = random.random() < RESPONSE_CHANCE

        if should_respond:
            async with message.channel.typing():
                # Gather context from channel history
                filled_messages = []
                current_tokens = 0
                MAX_TOKENS = genai.get_model(self.model_name).input_token_limit

                async for msg in message.channel.history(limit=MESSAGE_LIMIT, oldest_first=False):
                    tokens_count = self.model.count_tokens(msg.content).total_tokens if msg.content else 0
                    if current_tokens + tokens_count > MAX_TOKENS:
                        break
                    filled_messages.append(msg)
                    current_tokens += tokens_count

                messages_text = "\n".join(
                    f"{m.author}: {m.content}" for m in reversed(filled_messages)
                )
                prompt = prompt_template.format(messages=messages_text, name=self.bot.user.name, number=len(filled_messages))
                response = self.model.generate_content(prompt).text
                await message.channel.send(response)
                self.recent_chats[message.channel.id] = now
