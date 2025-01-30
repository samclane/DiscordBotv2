import random
import discord
from discord import app_commands
from discord.ext import commands
import os
import google.generativeai as genai

prompt_template = "You are a Discord bot who occasionally chimes in with insights and comments related to the conversation. " \
"Your name is {name}. " \
"Don't include your name in your response; Discord will handle that. " \
"Also don't include the messages or this prompt in your response. " \
"Here are the last 5 messages in chat:\n" \
"{messages}\n" \
"Put your response here: "

RESPONSE_CHANCE = .1
MESSAGE_LIMIT = 50
MODEL = "models/gemini-2.0-flash-exp"

@app_commands.guild_only()
class LLMCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        self.model_name = MODEL
        self.model = genai.GenerativeModel(self.model_name)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        if random.random() < RESPONSE_CHANCE:
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
            prompt = prompt_template.format(messages=messages_text, name=self.bot.user.name)
            response = self.model.generate_content(prompt).text
            await message.channel.send(response)