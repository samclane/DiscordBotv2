import asyncio
from io import BytesIO
import discord
from discord.ext import commands
from aiogtts import aiogTTS  # type: ignore
import aiohttp
import os
from audiofix import FFmpegPCMAudio
from collections import defaultdict
from enum import Enum, auto


class EventType(Enum):
    JOIN = auto()
    LEAVE = auto()
    AFK = auto()


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.aiogtts = aiogTTS()
        # Queue for each guild (guild_id -> list of (event_type, member, channel) tuples)
        self.voice_queues = defaultdict(list)
        # Active tasks that process voice queues (guild_id -> task)
        self.queue_tasks = {}
        # Batch timeout (seconds to wait before processing events)
        self.batch_timeout = 0.5

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
        await self.queue_event(EventType.JOIN, member, member.voice.channel)

    async def depart_user(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.queue_event(EventType.LEAVE, member, channel)

    async def afk_user(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.queue_event(EventType.AFK, member, channel)

    async def queue_event(self, event_type: EventType, member: discord.Member, channel: discord.VoiceChannel):
        """Queue an event for processing."""
        guild_id = channel.guild.id
        self.voice_queues[guild_id].append((event_type, member, channel))
        
        # Start a task to process the queue if not already running
        if guild_id not in self.queue_tasks or self.queue_tasks[guild_id].done():
            self.queue_tasks[guild_id] = asyncio.create_task(self.process_voice_queue(guild_id))

    async def process_voice_queue(self, guild_id: int):
        """Process and combine voice events in the queue for a specific guild."""
        # Wait a short time to allow events to accumulate
        await asyncio.sleep(self.batch_timeout)
        
        while self.voice_queues[guild_id]:
            # Group events by channel
            events_by_channel = defaultdict(list)
            
            # Copy the current queue to process
            current_events = self.voice_queues[guild_id].copy()
            self.voice_queues[guild_id] = []
            
            # Group events by channel, preserving the original order
            for event in current_events:
                event_type, member, channel = event
                events_by_channel[channel].append((event_type, member))
            
            # Process each channel's events in order
            for channel, channel_events in events_by_channel.items():
                # First, generate all messages while preserving order
                events_by_type = defaultdict(list)
                
                # Follow the original event order
                for event_type, member in channel_events:
                    events_by_type[event_type].append(member)
                
                messages = []
                
                # Generate messages for each event type that exists
                for event_type, members in events_by_type.items():
                    if event_type == EventType.JOIN:
                        if len(members) == 1:
                            messages.append(f"{members[0].name} has joined")
                        elif len(members) == 2:
                            messages.append(f"{members[0].name} and {members[1].name} have joined.")
                        else:
                            names = [m.name for m in members[:-1]]
                            last_name = members[-1].name
                            messages.append(f"{', '.join(names)}, and {last_name} have joined.")
                    
                    elif event_type == EventType.LEAVE:
                        if len(members) == 1:
                            messages.append(f"{members[0].name} has left")
                        elif len(members) == 2:
                            messages.append(f"{members[0].name} and {members[1].name} have left.")
                        else:
                            names = [m.name for m in members[:-1]]
                            last_name = members[-1].name
                            messages.append(f"{', '.join(names)}, and {last_name} have left.")
                    
                    elif event_type == EventType.AFK:
                        if len(members) == 1:
                            messages.append(f"{members[0].name} went A.F.K")
                        elif len(members) == 2:
                            messages.append(f"{members[0].name} and {members[1].name} went A.F.K.")
                        else:
                            names = [m.name for m in members[:-1]]
                            last_name = members[-1].name
                            messages.append(f"{', '.join(names)}, and {last_name} went A.F.K.")
                
                # Combine all messages for this channel
                if messages:
                    combined_message = ". ".join(messages)
                    try:
                        await self._play_voice_line(combined_message, channel, guild_id)
                    except Exception as e:
                        print(f"Error playing voice line: {e}")

    async def say_line(self, line: str, channel: discord.VoiceChannel):
        """Direct method for saying a line without event batching."""
        guild_id = channel.guild.id
        try:
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
