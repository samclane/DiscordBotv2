import asyncio
import itertools
import time
from io import BytesIO
import discord
from discord import app_commands
from discord.ext import commands
from aiogtts import aiogTTS  # type: ignore
import aiosqlite
import os
from elevenlabs.client import ElevenLabs
from audiofix import FFmpegStreamAudio
from collections import defaultdict
from enum import Enum, auto

# ElevenLabs voice/model configuration.
ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"
ELEVENLABS_MODEL_ID = "eleven_flash_v2_5"
# Streaming-friendly mp3 output; ffmpeg decodes it to Discord PCM on the fly.
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
# Both TTS paths emit mp3, so tell ffmpeg the format up front and skip input
# probing — otherwise it buffers incoming chunks before emitting any PCM.
FFMPEG_BEFORE_OPTIONS = "-f mp3 -analyzeduration 0 -probesize 32"
# Keep the voice connection warm after speaking so back-to-back announcements
# skip the connect handshake. Set to 0 to disconnect immediately.
IDLE_DISCONNECT_DELAY = 30.0
# Per-user settings (custom announced names), one sqlite file per cog like the
# other cogs (economy.db, whitelist.db, ...).
VOICE_DB = "voice.db"
# Users may change their announced name at most once every 30 days.
NAME_CHANGE_COOLDOWN = 30 * 24 * 60 * 60


class EventType(Enum):
    JOIN = auto()
    LEAVE = auto()
    AFK = auto()


# (singular, plural) verb phrases for announcement messages.
EVENT_PHRASES = {
    EventType.JOIN: ("has joined", "have joined."),
    EventType.LEAVE: ("has left", "have left."),
    EventType.AFK: ("went A.F.K", "went A.F.K."),
}


class VoiceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.aiogtts = aiogTTS()
        api_key = os.environ.get("XI_API_KEY")
        self.elevenlabs = ElevenLabs(api_key=api_key) if api_key else None
        # Queue for each guild (guild_id -> list of (event_type, member, channel) tuples)
        self.voice_queues = defaultdict(list)
        # Active tasks that process voice queues (guild_id -> task)
        self.queue_tasks = {}
        # Pending idle-disconnect timers (guild_id -> task)
        self.disconnect_timers = {}

    async def cog_load(self):
        async with aiosqlite.connect(VOICE_DB) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS voice_names ("
                "user_id INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL)"
            )
            # Cooldown timestamps live in their own table so clearing a name
            # (which deletes the voice_names row) can't reset the cooldown.
            await db.execute(
                "CREATE TABLE IF NOT EXISTS voice_name_cooldowns ("
                "user_id INTEGER PRIMARY KEY, "
                "changed_at INTEGER NOT NULL)"
            )
            await db.commit()

    def cog_unload(self):
        for timer in self.disconnect_timers.values():
            timer.cancel()
        self.disconnect_timers.clear()

    @app_commands.command()
    @app_commands.describe(name="Your new announced name (leave empty to go back to your username)")
    async def set_voice_name(
        self, interaction: discord.Interaction, name: str | None = None
    ):
        """Set the name voice announcements call you; omit it to use your username."""
        if name is None:
            async with aiosqlite.connect(VOICE_DB) as db:
                await db.execute(
                    "DELETE FROM voice_names WHERE user_id = ?", (interaction.user.id,)
                )
                await db.commit()
            await interaction.response.send_message(
                "Voice announcements will use your username.", ephemeral=True
            )
            return

        name = " ".join(name.split())
        if not name or len(name) > 32:
            await interaction.response.send_message(
                "Names must be 1-32 characters.", ephemeral=True
            )
            return

        now = int(time.time())
        async with aiosqlite.connect(VOICE_DB) as db:
            async with db.execute(
                "SELECT changed_at FROM voice_name_cooldowns WHERE user_id = ?",
                (interaction.user.id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and now - row[0] < NAME_CHANGE_COOLDOWN:
                next_change = row[0] + NAME_CHANGE_COOLDOWN
                await interaction.response.send_message(
                    "You can only change your voice name once a month. "
                    f"You can change it again <t:{next_change}:R> (<t:{next_change}:F>).",
                    ephemeral=True,
                )
                return
            await db.execute(
                "INSERT INTO voice_names (user_id, name) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
                (interaction.user.id, name),
            )
            await db.execute(
                "INSERT INTO voice_name_cooldowns (user_id, changed_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET changed_at = excluded.changed_at",
                (interaction.user.id, now),
            )
            await db.commit()
        await interaction.response.send_message(
            f'Voice announcements will call you "{name}".', ephemeral=True
        )

    async def _announced_names(self, members) -> dict[int, str]:
        """Map member id -> announced name, preferring user-set names from voice.db."""
        names = {m.id: m.name for m in members}
        if not names:
            return names
        placeholders = ", ".join("?" for _ in names)
        async with aiosqlite.connect(VOICE_DB) as db:
            async with db.execute(
                f"SELECT user_id, name FROM voice_names WHERE user_id IN ({placeholders})",
                list(names),
            ) as cursor:
                for user_id, custom_name in await cursor.fetchall():
                    names[user_id] = custom_name
        return names

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

        afk_channel = member.guild.afk_channel

        # Leaving a voice channel. Don't announce departures from the AFK
        # channel since it's muted and everyone inside is afk.
        if (
            before.channel is not None
            and not isinstance(before.channel, discord.StageChannel)
            and before.channel != afk_channel
        ):
            # Moving into the AFK channel: announce "went afk" in the channel
            # they left instead of a normal departure.
            if after.channel == afk_channel:
                await self.afk_user(member, before.channel)
            else:
                await self.depart_user(member, before.channel)

        # Joining a voice channel. Don't announce joins to the AFK channel.
        if after.channel is not None and after.channel != afk_channel:
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
        """Process and combine voice events in the queue for a specific guild.

        No upfront delay: the first line in a burst plays immediately, and any
        events arriving during TTS generation + playback are coalesced by the
        loop below on the next pass.
        """
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
            
            # Process the channel the bot is already sitting in first so a
            # batch that includes it never pays for an extra move-away/move-back.
            voice_client = discord.utils.get(self.bot.voice_clients, guild=self.bot.get_guild(guild_id))
            current_channel = voice_client.channel if voice_client and voice_client.is_connected() else None
            ordered_channels = sorted(
                events_by_channel.items(), key=lambda item: item[0] != current_channel
            )

            for channel, channel_events in ordered_channels:
                # First, generate all messages while preserving order
                events_by_type = defaultdict(list)

                # Follow the original event order
                for event_type, member in channel_events:
                    events_by_type[event_type].append(member)

                # Resolve custom announced names for the whole batch in one query.
                unique_members = {m.id: m for _, m in channel_events}
                names = await self._announced_names(unique_members.values())

                messages = []

                # Generate messages for each event type that exists
                for event_type, members in events_by_type.items():
                    display = [names[m.id] for m in members]
                    singular, plural = EVENT_PHRASES[event_type]
                    if len(display) == 1:
                        messages.append(f"{display[0]} {singular}")
                    elif len(display) == 2:
                        messages.append(f"{display[0]} and {display[1]} {plural}")
                    else:
                        messages.append(f"{', '.join(display[:-1])}, and {display[-1]} {plural}")
                
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

    async def _open_elevenlabs_stream(self, line: str):
        """Start an ElevenLabs streaming request and return a byte iterator.

        The first chunk is pulled eagerly (in a thread) so connection/auth errors
        surface here and we can fall back, while keeping the stream lazy afterwards.
        """
        if self.elevenlabs is None:
            raise RuntimeError("ElevenLabs API key not configured (XI_API_KEY)")

        def _start():
            stream = self.elevenlabs.text_to_speech.stream(
                ELEVENLABS_VOICE_ID,
                text=line,
                model_id=ELEVENLABS_MODEL_ID,
                output_format=ELEVENLABS_OUTPUT_FORMAT,
            )
            iterator = iter(stream)
            first_chunk = next(iterator)
            return itertools.chain([first_chunk], iterator)

        return await asyncio.to_thread(_start)

    async def _build_audio_source(self, line: str) -> FFmpegStreamAudio:
        """Build a streaming audio source, falling back to aiogTTS on failure."""
        try:
            byte_iterator = await self._open_elevenlabs_stream(line)
            return FFmpegStreamAudio(byte_iterator, before_options=FFMPEG_BEFORE_OPTIONS)
        except Exception as e:
            print(f"ElevenLabs streaming failed ({e}); falling back to aiogTTS")
            buffer = BytesIO()
            await self.aiogtts.write_to_fp(line, buffer)
            buffer.seek(0)
            return FFmpegStreamAudio(iter([buffer.read()]), before_options=FFMPEG_BEFORE_OPTIONS)

    async def _ensure_voice_client(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        """Return a voice client connected to the given channel, connecting or moving as needed."""
        voice_client = discord.utils.get(self.bot.voice_clients, guild=channel.guild)
        if voice_client and voice_client.is_connected():
            if voice_client.channel != channel:
                await voice_client.move_to(channel)
            return voice_client
        return await channel.connect()

    def _cancel_disconnect_timer(self, guild_id: int):
        timer = self.disconnect_timers.pop(guild_id, None)
        if timer is not None:
            timer.cancel()

    def _schedule_disconnect(self, guild_id: int, voice_client: discord.VoiceClient):
        """Disconnect after an idle period unless another line starts playing first."""
        self._cancel_disconnect_timer(guild_id)

        async def _disconnect_when_idle():
            await asyncio.sleep(IDLE_DISCONNECT_DELAY)
            if voice_client.is_connected() and not voice_client.is_playing():
                await voice_client.disconnect()

        self.disconnect_timers[guild_id] = asyncio.create_task(_disconnect_when_idle())

    async def _play_voice_line(self, line: str, channel: discord.VoiceChannel, guild_id: int):
        """Internal method that actually plays the voice line."""
        self._cancel_disconnect_timer(guild_id)

        # The TTS request and the voice connect handshake are independent network
        # round-trips; run them concurrently so we only wait for the slower one.
        source, voice_client = await asyncio.gather(
            self._build_audio_source(line),
            self._ensure_voice_client(channel),
            return_exceptions=True,
        )
        if isinstance(voice_client, BaseException):
            if not isinstance(source, BaseException):
                source.cleanup()
            raise voice_client
        if isinstance(source, BaseException):
            raise source

        loop = asyncio.get_running_loop()
        finished = asyncio.Event()

        def _on_playback_done(error):
            if error:
                print(f"Voice playback error: {error}")
            loop.call_soon_threadsafe(finished.set)

        voice_client.play(source, after=_on_playback_done)
        await finished.wait()

        # Keep the connection warm briefly in case more announcements follow.
        if len(self.voice_queues[guild_id]) == 0:
            self._schedule_disconnect(guild_id, voice_client)
