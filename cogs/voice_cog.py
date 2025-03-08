import asyncio
from io import BytesIO
import discord
from discord.ext import commands
from aiogtts import aiogTTS  # type: ignore
import aiohttp
import os
from audiofix import FFmpegPCMAudio
from collections import defaultdict


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.aiogtts = aiogTTS()
        # Queue for each guild (guild_id -> list of (text, channel) tuples)
        self.voice_queues = defaultdict(list)
        # Active tasks that process voice queues (guild_id -> task)
        self.queue_tasks = {}

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # Ignore the bot itself and other bots
        if member == self.bot.user or member.bot:
            return

        # Ignore if the user is just muted or deafened
        if before.channel == after.channel:
            return

        # Leaving a voice channel or going afk
        if before.channel is not None and not isinstance(
            before.channel, discord.StageChannel
        ):
            if after.channel == before.channel.guild.afk_channel:
                await self.afk_user(member, before.channel)
            else:
                await self.depart_user(member, before.channel)

        # Joining a voice channel
        if after.channel is not None:
            await self.greet_user(member)

    async def greet_user(self, member: discord.Member):
        if (
            member.voice is None
            or member.voice.channel is None
            or isinstance(member.voice.channel, discord.StageChannel)
        ):
            return
        await self.say_line(f"{member.name} has joined.", member.voice.channel)

    async def depart_user(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.say_line(f"{member.name} has left.", channel)

    async def afk_user(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.say_line(f"{member.name} went afk", channel)

    async def say_line(self, line: str, channel: discord.VoiceChannel):
        # Add the request to the queue
        guild_id = channel.guild.id
        self.voice_queues[guild_id].append((line, channel))
        
        # Start a task to process the queue if not already running
        if guild_id not in self.queue_tasks or self.queue_tasks[guild_id].done():
            self.queue_tasks[guild_id] = asyncio.create_task(self.process_voice_queue(guild_id))

    async def process_voice_queue(self, guild_id: int):
        """Process all voice lines in the queue for a specific guild."""
        while self.voice_queues[guild_id]:
            line, channel = self.voice_queues[guild_id][0]
            
            try:
                # Remove the processed item before playing
                # This ensures we know exactly what's left in the queue when deciding to disconnect
                self.voice_queues[guild_id].pop(0)
                await self._play_voice_line(line, channel, guild_id)
            except Exception as e:
                print(f"Error playing voice line: {e}")

    async def _play_voice_line(self, line: str, channel: discord.VoiceChannel, guild_id: int):
        """Internal method that actually plays the voice line."""
        buffer = BytesIO()

        tts_url = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}".format(
            voice_id="EXAVITQu4vr4xnSDxMaL"
        )
        model_id = "eleven_flash_v2"
        formatted_message = {
            "model_id": model_id,
            "text": line,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                tts_url,
                headers={
                    "Content-Type": "application/json",
                    "xi-api-key": os.environ["XI_API_KEY"],
                },
                json=formatted_message,
            ) as response:
                if response.status == 200:
                    buffer = BytesIO(await response.read())
                else:
                    print(
                        "ElevenLabs Request failed with status code:", response.status
                    )
                    print("Response content:", await response.text())
                    print("Falling back to aiogTTS")
                    await self.aiogtts.write_to_fp(line, buffer)

        buffer.seek(0)

        voice_guild = channel.guild
        if voice_guild is not None:
            # Check if we're already connected to a voice channel in this guild
            voice_client = discord.utils.get(self.bot.voice_clients, guild=voice_guild)
            if voice_client and voice_client.is_connected():
                if voice_client.channel != channel:
                    await voice_client.move_to(channel)
            else:
                voice_client = await channel.connect()

            voice_client.play(FFmpegPCMAudio(buffer.read(), pipe=True))

            # Wait for the audio to finish playing
            while voice_client.is_playing():
                await asyncio.sleep(0.5)
                
            # Check the queue length after playing the current line
            if len(self.voice_queues[guild_id]) == 0:
                await voice_client.disconnect()
        else:
            await channel.send("You need to join a voice channel first!")
