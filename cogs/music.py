import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import KLIPY_API_KEY
from services.music.ipc import iter_music_commands
from services.music.player import MusicPlayer
from services.music.source import LocalFileSource, YTDLSource


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("bot_service.music")
        self.player = MusicPlayer(bot)
        self.web_task = asyncio.create_task(self.check_web_commands())
        self.command_idle_sleep = 0.1
        self.command_busy_sleep = 0.01
        self.web_command_tasks = set()

    def cog_unload(self):
        if self.web_task:
            self.web_task.cancel()
        for task in list(self.web_command_tasks):
            task.cancel()

    def get_voice_client(self, guild_id: int):
        for voice_client in self.bot.voice_clients:
            if voice_client.guild and voice_client.guild.id == guild_id:
                return voice_client
        return None

    def _safe_filename(self, filename: str) -> str:
        original = Path(filename).name or "audio"
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", original)
        sanitized = sanitized.strip("._") or "audio"
        return sanitized[:120]

    def _build_temp_path(self, filename: str) -> Path:
        os.makedirs("temp", exist_ok=True)
        safe_name = self._safe_filename(filename)
        return Path("temp") / f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}"

    def _get_klipy_api_key(self) -> str:
        if os.path.exists(".env"):
            try:
                with open(".env", "r", encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue

                        key, value = line.split("=", 1)
                        if key in {"KLIPY_API_KEY", "TENOR_API_KEY"} and value.strip():
                            return value.strip().strip("\"'")
            except OSError:
                pass

        env_value = (os.getenv("KLIPY_API_KEY") or os.getenv("TENOR_API_KEY") or "").strip()
        if env_value:
            return env_value.strip("\"'")

        return (KLIPY_API_KEY or "").strip().strip("\"'")

    def _parse_index(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_move_query(self, value):
        if isinstance(value, str):
            raw = value.strip()
            if ":" in raw:
                left, right = raw.split(":", 1)
                from_index = self._parse_index(left.strip())
                to_index = self._parse_index(right.strip())
                if from_index is not None and to_index is not None:
                    return from_index, to_index
            return None

        if isinstance(value, dict):
            from_index = self._parse_index(value.get("from"))
            to_index = self._parse_index(value.get("to"))
            if from_index is not None and to_index is not None:
                return from_index, to_index
        return None

    def _queue_entry_title(self, entry) -> str:
        if entry is None:
            return "Unknown track"
        player = getattr(entry, "player", None)
        if player and getattr(player, "title", None):
            return player.title
        history_data = getattr(entry, "history_data", None) or {}
        if history_data.get("song_title"):
            return str(history_data["song_title"])
        query = getattr(entry, "query", None)
        if not query:
            return "Pending track"
        source_type = getattr(entry, "source_type", None)
        if source_type == "local":
            return os.path.basename(str(query))
        if self._looks_like_url(str(query)):
            return "Resolving track..."
        return str(query)

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _build_web_history_data(
        self,
        guild_id: int,
        query: str,
        source_type: str,
        history_seed: dict | None,
    ):
        seed = history_seed if isinstance(history_seed, dict) else {}

        seed_title = str(seed.get("song_title") or "").strip()
        seed_url = str(seed.get("song_url") or "").strip()
        seed_query = str(seed.get("song_query") or "").strip()
        seed_thumbnail = seed.get("thumbnail_url")
        seed_duration = seed.get("song_duration")
        if source_type == "local":
            fallback_title = seed_title or os.path.basename(query)
            fallback_query = seed_query or os.path.basename(query)
        else:
            fallback_title = seed_title or ("Queued track" if self._looks_like_url(query) else query)
            fallback_query = seed_query or query

        if source_type == "local":
            fallback_url = seed_url or query
        else:
            fallback_url = seed_url or query

        if seed_duration is not None:
            try:
                seed_duration = max(0, int(float(seed_duration)))
            except (TypeError, ValueError):
                seed_duration = None

        bot_user = self.bot.user
        user_id = bot_user.id if bot_user else 0

        return self.player.prepare_history_data(
            guild_id=guild_id,
            song_title=fallback_title,
            song_url=fallback_url,
            thumbnail_url=seed_thumbnail,
            user_id=user_id,
            username="Web UI",
            user_avatar_url=None,
            song_duration=seed_duration,
            source_type=source_type,
            query=fallback_query,
        )

    def _schedule_web_task(self, coro):
        task = self.bot.loop.create_task(coro)
        self.web_command_tasks.add(task)

        def _cleanup(done_task):
            self.web_command_tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc:
                self.logger.error(
                    "Web command task failed: %s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_cleanup)
        return task

    async def _resolve_queued_youtube_metadata(self, guild_id: int, queue_entry, query: str):
        try:
            info = await YTDLSource.fetch_info(query, loop=self.bot.loop)
        except Exception as exc:
            self.logger.error("Failed to resolve queued metadata for guild %s: %s", guild_id, exc)
            return

        if queue_entry.history_data is None:
            queue_entry.history_data = {}

        current_title = str(queue_entry.history_data.get("song_title") or "").strip()
        should_replace_title = (
            not current_title
            or current_title == "Queued track"
            or current_title == query
            or self._looks_like_url(current_title)
        )
        if should_replace_title:
            queue_entry.history_data["song_title"] = info.get("title") or current_title or query
        queue_entry.history_data["song_url"] = info.get("url") or queue_entry.history_data.get("song_url") or query
        if not queue_entry.history_data.get("thumbnail_url"):
            queue_entry.history_data["thumbnail_url"] = info.get("thumbnail")
        if queue_entry.history_data.get("song_duration") is None:
            queue_entry.history_data["song_duration"] = info.get("duration")
        self.player.update_state(guild_id)

    async def ensure_voice_client(self, interaction: discord.Interaction):
        if interaction.guild is None:
            raise RuntimeError("This command can only be used in a server.")

        user_voice = getattr(interaction.user, "voice", None)
        if not user_voice or not user_voice.channel:
            raise RuntimeError("You need to be in a voice channel!")

        voice_client = self.get_voice_client(interaction.guild.id)
        if voice_client and voice_client.is_connected():
            if voice_client.channel != user_voice.channel:
                await voice_client.move_to(user_voice.channel)
            return voice_client

        try:
            voice_client = await user_voice.channel.connect(self_deaf=True)
        except discord.ClientException:
            voice_client = self.get_voice_client(interaction.guild.id)
            if not voice_client or not voice_client.is_connected():
                raise RuntimeError("Failed to connect to your voice channel.")
            if voice_client.channel != user_voice.channel:
                await voice_client.move_to(user_voice.channel)
            return voice_client
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Voice connection timed out. Please try again.") from exc

        if not voice_client or not voice_client.is_connected():
            voice_client = self.get_voice_client(interaction.guild.id)

        if not voice_client or not voice_client.is_connected():
            raise RuntimeError("Failed to connect to your voice channel.")

        return voice_client

    async def check_web_commands(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            processed_any = False
            try:
                for command in iter_music_commands():
                    processed_any = True
                    try:
                        await self._handle_web_command(command)
                    except Exception as exc:
                        self.logger.error(
                            "Web command error: %s",
                            exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
            except Exception as exc:
                self.logger.error(
                    "Web command check error: %s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

            await asyncio.sleep(self.command_busy_sleep if processed_any else self.command_idle_sleep)

    async def _handle_web_command(self, command):
        try:
            guild_id = int(command.get("guild_id"))
        except (TypeError, ValueError):
            self.logger.warning("Discarding web command without valid guild_id: %s", command)
            return

        command_type = command.get("type")
        query = command.get("query")
        history_seed = command.get("history_data")
        voice_client = self.get_voice_client(guild_id)

        try:
            if command_type == "play":
                self._schedule_web_task(self.handle_web_play(guild_id, query, history_seed=history_seed))
            elif command_type == "play_file":
                self._schedule_web_task(self.handle_web_play_file(guild_id, query, history_seed=history_seed))
            elif command_type == "stop":
                if voice_client:
                    self.player.stop(guild_id, voice_client)
                    self.player.start_idle_timer(guild_id, voice_client)
            elif command_type == "skip":
                if voice_client:
                    self.player.skip(guild_id, voice_client)
            elif command_type == "pause":
                if voice_client:
                    self.player.pause(guild_id, voice_client)
            elif command_type == "resume":
                if voice_client:
                    self.player.resume(guild_id, voice_client)
            elif command_type == "remove":
                index = self._parse_index(query)
                if index is not None:
                    self.player.remove_at(guild_id, index)
            elif command_type == "move":
                move_indices = self._parse_move_query(query)
                if move_indices is not None:
                    from_index, to_index = move_indices
                    self.player.move_at(guild_id, from_index, to_index)
            elif command_type == "skipto":
                index = self._parse_index(query)
                if voice_client and index is not None:
                    await self.player.skip_to(guild_id, voice_client, index)
            elif command_type == "shuffle":
                self.player.shuffle(guild_id)
            elif command_type == "clear":
                self.player.clear_queue(guild_id)
        except Exception as exc:
            self.player.set_error(guild_id, str(exc))
            raise

    async def handle_web_play(self, guild_id: int, query: str, history_seed: dict | None = None):
        query = (query or "").strip()
        if not query:
            self.player.set_error(guild_id, "Provide a song URL or search query.")
            return

        try:
            voice_client = self.get_voice_client(guild_id)
            if not voice_client or not voice_client.is_connected():
                self.logger.debug("Web play ignored for guild %s: Bot not in voice channel", guild_id)
                self.player.set_error(
                    guild_id,
                    "Bot is not in a voice channel for this server. Join a channel first or use /join.",
                )
                return

            self.player.start_session(guild_id)
            if voice_client.is_playing() or voice_client.is_paused():
                history_data = self._build_web_history_data(
                    guild_id=guild_id,
                    query=query,
                    source_type="youtube",
                    history_seed=history_seed,
                )
                queue_entry = self.player.add_to_queue(
                    guild_id,
                    player=None,
                    query=query,
                    history_data=history_data,
                    source_type="youtube",
                )
                self._schedule_web_task(self._resolve_queued_youtube_metadata(guild_id, queue_entry, query))
                return

            player = await YTDLSource.from_url(query, loop=self.bot.loop, stream=True)
            if player is None:
                raise RuntimeError("No playable source was returned for this track.")
            history_data = self.player.prepare_history_data(
                guild_id=guild_id,
                song_title=getattr(player, "title", None) or (history_seed or {}).get("song_title") or query,
                song_url=getattr(player, "webpage_url", None) or player.url,
                thumbnail_url=getattr(player, "thumbnail", None) or (history_seed or {}).get("thumbnail_url"),
                user_id=self.bot.user.id if self.bot.user else 0,
                username="Web UI",
                user_avatar_url=None,
                song_duration=getattr(player, "duration", None),
                source_type="youtube",
                query=(history_seed or {}).get("song_query") or query,
            )

            if voice_client.is_playing() or voice_client.is_paused():
                queue_entry = self.player.add_to_queue(
                    guild_id,
                    player=None,
                    query=query,
                    history_data=history_data,
                    source_type="youtube",
                )
                self._schedule_web_task(self._resolve_queued_youtube_metadata(guild_id, queue_entry, query))
                return

            self.player.play_song(
                guild_id,
                None,
                voice_client,
                player,
                query=query,
                history_data=history_data,
            )
        except Exception as exc:
            self.player.set_error(guild_id, str(exc))
            raise

    async def handle_web_play_file(self, guild_id: int, filepath: str, history_seed: dict | None = None):
        filepath = (filepath or "").strip()
        if not filepath:
            self.player.set_error(guild_id, "No local file path was provided.")
            return

        try:
            if not os.path.exists(filepath):
                self.player.set_error(guild_id, "The selected local file does not exist anymore.")
                return

            voice_client = self.get_voice_client(guild_id)
            if not voice_client or not voice_client.is_connected():
                self.player.set_error(guild_id, "Bot is not in a voice channel for this server.")
                return

            self.player.start_session(guild_id)
            if voice_client.is_playing() or voice_client.is_paused():
                history_data = self._build_web_history_data(
                    guild_id=guild_id,
                    query=filepath,
                    source_type="local",
                    history_seed=history_seed,
                )
                self.player.add_to_queue(
                    guild_id,
                    player=None,
                    query=filepath,
                    history_data=history_data,
                    source_type="local",
                )
                return

            player = await LocalFileSource.from_path(filepath, loop=self.bot.loop)
            if player is None:
                raise RuntimeError("No playable local source was returned.")
            history_data = self.player.prepare_history_data(
                guild_id=guild_id,
                song_title=os.path.basename(filepath),
                song_url=filepath,
                thumbnail_url=None,
                user_id=self.bot.user.id if self.bot.user else 0,
                username="Web UI",
                user_avatar_url=None,
                song_duration=getattr(player, "duration", None),
                source_type="local",
                query=(history_seed or {}).get("song_query") or os.path.basename(filepath),
            )

            self.player.play_song(
                guild_id,
                None,
                voice_client,
                player,
                query=filepath,
                history_data=history_data,
            )
        except Exception as exc:
            self.player.set_error(guild_id, str(exc))
            raise

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        try:
            voice_client = await self.ensure_voice_client(interaction)
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        self.player.start_session(interaction.guild.id)
        await interaction.response.send_message(f"Joined {voice_client.channel.name}")

    @app_commands.command(name="leave", description="Leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        voice_client = self.get_voice_client(guild.id)
        if not voice_client:
            await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)
            return

        self.player.stop(guild.id, voice_client)
        await voice_client.disconnect()
        self.player.end_session(guild.id)
        await interaction.response.send_message("Disconnected from voice channel.")

    @app_commands.command(name="play", description="Play a song from YouTube")
    @app_commands.describe(query="YouTube URL or search query")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        try:
            voice_client = await self.ensure_voice_client(interaction)
            self.player.start_session(guild.id)

            user_avatar = interaction.user.display_avatar.url if interaction.user.display_avatar else None
            if voice_client.is_playing() or voice_client.is_paused():
                info = await YTDLSource.fetch_info(query, loop=self.bot.loop)
                history_data = self.player.prepare_history_data(
                    guild_id=guild.id,
                    song_title=info.get("title") or query,
                    song_url=info.get("url") or query,
                    thumbnail_url=info.get("thumbnail"),
                    user_id=interaction.user.id,
                    username=str(interaction.user),
                    user_avatar_url=user_avatar,
                    song_duration=info.get("duration"),
                    source_type="youtube",
                    query=query,
                )
                self.player.add_to_queue(
                    guild.id,
                    player=None,
                    query=query,
                    history_data=history_data,
                    source_type="youtube",
                )
                await interaction.followup.send(f"Added to queue: **{info.get('title') or query}**")
            else:
                player = await YTDLSource.from_url(query, loop=self.bot.loop, stream=True)
                if player is None:
                    raise RuntimeError("No playable source was returned for this track.")
                history_data = self.player.prepare_history_data(
                    guild_id=guild.id,
                    song_title=getattr(player, "title", None) or query,
                    song_url=getattr(player, "webpage_url", None) or player.url,
                    thumbnail_url=getattr(player, "thumbnail", None),
                    user_id=interaction.user.id,
                    username=str(interaction.user),
                    user_avatar_url=user_avatar,
                    song_duration=getattr(player, "duration", None),
                    source_type="youtube",
                    query=query,
                )
                started = self.player.play_song(
                    guild.id,
                    None,
                    voice_client,
                    player,
                    query=query,
                    history_data=history_data,
                )
                if not started:
                    raise RuntimeError("Failed to start playback.")
                await interaction.followup.send(
                    f"Now playing: **{getattr(player, 'title', None) or query}**"
                )
        except Exception as exc:
            self.logger.error("Play error: %s", exc)
            if guild is not None:
                self.player.set_error(guild.id, str(exc))
            await interaction.followup.send(f"Error: {exc}")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        voice_client = self.get_voice_client(guild.id)
        if self.player.pause(guild.id, voice_client):
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message("Nothing is playing!", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        voice_client = self.get_voice_client(guild.id)
        if self.player.resume(guild.id, voice_client):
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused!", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playing and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        guild = interaction.guild
        voice_client = self.get_voice_client(guild.id) if guild else None
        if voice_client:
            self.player.stop(guild.id, voice_client)
            self.player.start_idle_timer(guild.id, voice_client)
            await interaction.response.send_message("Stopped and cleared queue.")
        else:
            await interaction.response.send_message("Not playing anything!", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        guild = interaction.guild
        voice_client = self.get_voice_client(guild.id) if guild else None
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            self.player.skip(guild.id, voice_client)
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing!", ephemeral=True)

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction):
        guild = interaction.guild
        queue_items = self.player.get_queue(guild.id) if guild else []
        if not queue_items:
            await interaction.response.send_message("Queue is empty!")
            return

        embed = discord.Embed(title="Music Queue", color=discord.Color.blue())
        for index, entry in enumerate(queue_items[:10], start=1):
            embed.add_field(name=f"{index}. {self._queue_entry_title(entry)}", value="", inline=False)

        if len(queue_items) > 10:
            embed.set_footer(text=f"And {len(queue_items) - 10} more...")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        guild = interaction.guild
        queue_items = self.player.get_queue(guild.id) if guild else []
        if not queue_items:
            await interaction.response.send_message("Queue is empty!", ephemeral=True)
            return

        self.player.shuffle(guild.id)
        await interaction.response.send_message("Queue shuffled.")

    @app_commands.command(name="clear", description="Clear the queue")
    async def clear(self, interaction: discord.Interaction):
        guild = interaction.guild
        self.player.clear_queue(guild.id)
        await interaction.response.send_message("Queue cleared.")

    @app_commands.command(name="remove", description="Remove a song from the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def remove(self, interaction: discord.Interaction, position: int):
        guild = interaction.guild
        removed = self.player.remove_at(guild.id, position - 1) if guild else None
        if removed:
            await interaction.response.send_message(f"Removed: **{self._queue_entry_title(removed)}**")
        else:
            await interaction.response.send_message("Invalid position!", ephemeral=True)

    @app_commands.command(name="promote", description="Move a song to the front of the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def promote(self, interaction: discord.Interaction, position: int):
        guild = interaction.guild
        promoted = self.player.promote_at(guild.id, position - 1) if guild else None
        if promoted:
            await interaction.response.send_message(f"Promoted: **{self._queue_entry_title(promoted)}**")
        else:
            await interaction.response.send_message("Invalid position!", ephemeral=True)

    @app_commands.command(name="playfile", description="Play a local audio file")
    @app_commands.describe(file="Audio file to play")
    async def playfile(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer()

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command can only be used in a server.")
            return

        try:
            voice_client = await self.ensure_voice_client(interaction)
            self.player.start_session(guild.id)

            filepath = self._build_temp_path(file.filename)
            await file.save(filepath)

            user_avatar = interaction.user.display_avatar.url if interaction.user.display_avatar else None
            if voice_client.is_playing() or voice_client.is_paused():
                queued_history = self.player.prepare_history_data(
                    guild_id=guild.id,
                    song_title=file.filename,
                    song_url=str(filepath),
                    thumbnail_url=None,
                    user_id=interaction.user.id,
                    username=str(interaction.user),
                    user_avatar_url=user_avatar,
                    song_duration=None,
                    source_type="local",
                    query=file.filename,
                )
                self.player.add_to_queue(
                    guild.id,
                    player=None,
                    query=str(filepath),
                    history_data=queued_history,
                    source_type="local",
                )
                await interaction.followup.send(f"Added to queue: **{file.filename}**")
            else:
                player = await LocalFileSource.from_path(str(filepath), loop=self.bot.loop)
                if player is None:
                    raise RuntimeError("No playable local source was returned.")
                history_data = self.player.prepare_history_data(
                    guild_id=guild.id,
                    song_title=file.filename,
                    song_url=str(filepath),
                    thumbnail_url=None,
                    user_id=interaction.user.id,
                    username=str(interaction.user),
                    user_avatar_url=user_avatar,
                    song_duration=getattr(player, "duration", None),
                    source_type="local",
                    query=file.filename,
                )
                started = self.player.play_song(
                    guild.id,
                    None,
                    voice_client,
                    player,
                    query=file.filename,
                    history_data=history_data,
                )
                if not started:
                    raise RuntimeError("Failed to start playback.")
                await interaction.followup.send(
                    f"Now playing: **{getattr(player, 'title', None) or file.filename}**"
                )
        except Exception as exc:
            self.logger.error("Playfile error: %s", exc)
            if guild is not None:
                self.player.set_error(guild.id, str(exc))
            await interaction.followup.send(f"Error: {exc}")

    @app_commands.command(name="setidle", description="Set idle disconnect timeout")
    @app_commands.describe(minutes="Minutes before auto-disconnect (0 to disable)")
    async def setidle(self, interaction: discord.Interaction, minutes: int):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        self.player.set_idle_timeout(guild.id, minutes)
        if minutes > 0:
            await interaction.response.send_message(f"Idle timeout set to {minutes} minutes.")
        else:
            await interaction.response.send_message("Idle timeout disabled.")

    @app_commands.command(name="skipto", description="Skip to a specific song in the queue")
    @app_commands.describe(position="Position in queue (1-based)")
    async def skipto(self, interaction: discord.Interaction, position: int):
        guild = interaction.guild
        voice_client = self.get_voice_client(guild.id) if guild else None
        if not voice_client:
            await interaction.response.send_message("Nothing is playing!", ephemeral=True)
            return

        success = await self.player.skip_to(guild.id, voice_client, position - 1)
        if success:
            await interaction.response.send_message(f"Skipped to position {position}.")
        else:
            await interaction.response.send_message("Invalid position!", ephemeral=True)

    @app_commands.command(name="help", description="Show all available music commands")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Music Bot Commands",
            description="All available music commands",
            color=discord.Color.green(),
        )

        commands_info = [
            ("/play [query]", "Play a song from YouTube"),
            ("/pause", "Pause the current song"),
            ("/resume", "Resume playback"),
            ("/stop", "Stop and clear queue"),
            ("/skip", "Skip current song"),
            ("/queue", "Show the queue"),
            ("/shuffle", "Shuffle the queue"),
            ("/clear", "Clear the queue"),
            ("/remove [pos]", "Remove song at position"),
            ("/promote [pos]", "Move song to front"),
            ("/playfile [file]", "Play a local file"),
            ("/setidle [min]", "Set idle timeout"),
            ("/join", "Join your voice channel"),
            ("/leave", "Leave voice channel"),
            ("/rat", "Random rat GIF"),
        ]

        for command_name, description in commands_info:
            embed.add_field(name=command_name, value=description, inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rat", description="Get a random rat GIF")
    async def rat(self, interaction: discord.Interaction):
        await interaction.response.defer()

        klipy_api_key = self._get_klipy_api_key()
        if not klipy_api_key:
            await interaction.followup.send("KLIPY API key not configured!", ephemeral=True)
            return

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                params = {
                    "q": "rat",
                    "key": klipy_api_key,
                    "client_key": "theratbot",
                    "limit": "50",
                    "random": "true",
                }
                async with session.get("https://api.klipy.com/v2/search", params=params) as response:
                    if response.status != 200:
                        body_preview = (await response.text())[:180]
                        raise RuntimeError(
                            f"KLIPY returned status {response.status}: {body_preview}"
                        )

                    data = await response.json()
                    results = data.get("results") or []
                    if not results:
                        await interaction.followup.send("No rats found.")
                        return

                    gif = results[0]
                    media_formats = gif.get("media_formats", {})
                    gif_url = None
                    for format_name in ("gif", "tinygif", "mediumgif", "nanogif"):
                        gif_data = media_formats.get(format_name) or {}
                        if gif_data.get("url"):
                            gif_url = gif_data["url"]
                            break

                    if not gif_url:
                        gif_url = gif.get("url") or gif.get("itemurl")
                    if not gif_url:
                        raise RuntimeError("KLIPY response did not include a GIF URL.")

                    embed = discord.Embed(
                        title="Random Rat GIF",
                        color=discord.Color.blurple(),
                    )
                    embed.set_image(url=gif_url)
                    embed.set_footer(text="Powered by KLIPY")
                    await interaction.followup.send(embed=embed)
        except Exception as exc:
            self.logger.error("Rat command error: %s", exc)
            await interaction.followup.send("Error fetching rat GIF.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Music(bot))
