from __future__ import annotations

import asyncio
import datetime
import logging
import os
import random
import time
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from database import MusicHistory, db_manager
from services.music.ipc import save_music_state


@dataclass
class QueueEntry:
    player: Optional[Any] = None
    query: Optional[str] = None
    history_data: Optional[Dict[str, Any]] = None
    source_type: str = "youtube"


@dataclass
class GuildPlaybackState:
    queue: List[QueueEntry] = field(default_factory=list)
    forced_next_entry: Optional[QueueEntry] = None
    current_track: Optional[str] = None
    current_thumbnail: Optional[str] = None
    current_query: Optional[str] = None
    current_duration: Optional[float] = None
    current_start_time: Optional[datetime.datetime] = None
    current_elapsed: float = 0
    current_history_data: Optional[Dict[str, Any]] = None
    is_playing: bool = False
    is_paused: bool = False
    idle_timeout: int = 30 * 60
    idle_task: Optional[asyncio.Task] = None
    progress_task: Optional[asyncio.Task] = None
    error_clear_task: Optional[asyncio.Task] = None
    last_error: Optional[str] = None
    session_start_time: Optional[float] = None
    last_update: float = 0
    history_version: int = 0


class MusicPlayer:
    def __init__(self, bot):
        self.bot = bot
        self.guild_states: Dict[int, GuildPlaybackState] = {}
        self.logger = logging.getLogger("services.music.player")

    @staticmethod
    def _looks_like_url(value: Optional[str]) -> bool:
        if not value:
            return False
        try:
            parsed = urlparse(str(value))
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _get_state(self, guild_id: int) -> GuildPlaybackState:
        guild_id = int(guild_id)
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildPlaybackState()
            self.update_state(guild_id)
        return self.guild_states[guild_id]

    @staticmethod
    def _entry_title(entry: QueueEntry) -> str:
        if entry.player and getattr(entry.player, "title", None):
            return entry.player.title
        if entry.history_data and entry.history_data.get("song_title"):
            return str(entry.history_data["song_title"])
        if entry.query:
            if entry.source_type == "local":
                return os.path.basename(str(entry.query))
            if MusicPlayer._looks_like_url(entry.query):
                return "Resolving track..."
            return str(entry.query)
        return "Pending track"

    @staticmethod
    def _entry_thumbnail(entry: QueueEntry) -> Optional[str]:
        if entry.player and getattr(entry.player, "thumbnail", None):
            return entry.player.thumbnail
        if entry.history_data:
            return entry.history_data.get("thumbnail_url")
        return None

    def get_queue(self, guild_id: int) -> List[QueueEntry]:
        return list(self._get_state(guild_id).queue)

    def _is_voice_active(self, voice_client) -> bool:
        return bool(voice_client and (voice_client.is_playing() or voice_client.is_paused()))

    def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()

    def _reset_current_track(self, state: GuildPlaybackState) -> None:
        state.current_track = None
        state.current_thumbnail = None
        state.current_query = None
        state.current_duration = None
        state.current_start_time = None
        state.current_elapsed = 0
        state.current_history_data = None
        state.is_playing = False
        state.is_paused = False

    def _get_elapsed_seconds(self, state: GuildPlaybackState) -> float:
        elapsed = max(0.0, state.current_elapsed)
        if state.is_playing and state.current_start_time:
            elapsed += max(
                0.0,
                (datetime.datetime.utcnow() - state.current_start_time).total_seconds(),
            )
        if state.current_duration and state.current_duration > 0:
            return min(elapsed, state.current_duration)
        return elapsed

    def start_session(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        if not state.session_start_time:
            state.session_start_time = time.time()
            self.update_state(guild_id)

    def end_session(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        self.cancel_idle_timer(guild_id)
        state.session_start_time = None
        self.update_state(guild_id)

    def set_error(self, guild_id: int, error_msg: str) -> None:
        state = self._get_state(guild_id)
        state.last_error = error_msg
        self.update_state(guild_id)

        self._cancel_task(state.error_clear_task)
        state.error_clear_task = asyncio.create_task(self._clear_error_after(guild_id, 5))

    async def _clear_error_after(self, guild_id: int, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return

        state = self._get_state(guild_id)
        state.last_error = None
        self.update_state(guild_id)

    async def _record_history_async(self, history_data: Dict[str, Any]) -> None:
        guild_id = history_data.get("guild_id")
        if not guild_id:
            self.logger.warning("Missing guild_id in history payload; skipping history write")
            return

        try:
            async with db_manager.get_session(guild_id)() as session:
                history_entry = MusicHistory(**{
                    key: value
                    for key, value in history_data.items()
                    if key != "guild_id"
                })
                session.add(history_entry)
                await session.commit()
                self.logger.debug(
                    "Recorded history for guild %s: %s",
                    guild_id,
                    history_data.get("song_title"),
                )
        except Exception as exc:
            self.logger.error("Failed to record history for guild %s: %s", guild_id, exc)

        state = self._get_state(guild_id)
        state.history_version += 1
        self.update_state(guild_id)

    def prepare_history_data(
        self,
        guild_id: int,
        song_title: str,
        song_url: str,
        thumbnail_url: Optional[str],
        user_id: int,
        username: str,
        user_avatar_url: Optional[str],
        song_duration: Optional[float] = None,
        source_type: str = "youtube",
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        if source_type == "local":
            song_title = os.path.basename(song_url)
            query = query or song_title

        duration_seconds = None
        if song_duration is not None:
            try:
                duration_seconds = max(0, int(round(float(song_duration))))
            except (TypeError, ValueError):
                duration_seconds = None

        return {
            "guild_id": guild_id,
            "user_id": user_id,
            "username": username,
            "user_avatar_url": user_avatar_url,
            "song_title": song_title,
            "song_url": song_url,
            "song_query": query,
            "song_duration": duration_seconds,
            "thumbnail_url": thumbnail_url,
            "source_type": source_type,
            "timestamp": datetime.datetime.utcnow(),
        }

    def set_idle_timeout(self, guild_id: int, minutes: int) -> None:
        state = self._get_state(guild_id)
        state.idle_timeout = max(0, minutes) * 60

    def start_idle_timer(self, guild_id: int, voice_client) -> None:
        state = self._get_state(guild_id)
        if state.idle_timeout <= 0:
            return

        self.cancel_idle_timer(guild_id)
        state.idle_task = asyncio.create_task(self._idle_disconnect(guild_id, voice_client))

    def cancel_idle_timer(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        self._cancel_task(state.idle_task)
        state.idle_task = None

    async def _idle_disconnect(self, guild_id: int, voice_client) -> None:
        state = self._get_state(guild_id)
        try:
            await asyncio.sleep(state.idle_timeout)
            if voice_client and voice_client.is_connected():
                self.stop(guild_id, voice_client)
                await voice_client.disconnect()
                self.end_session(guild_id)
                self.logger.info("Disconnected from guild %s due to inactivity", guild_id)
        except asyncio.CancelledError:
            pass

    def update_state(self, guild_id: int) -> None:
        state = self._get_state(guild_id)

        progress = 0
        elapsed = self._get_elapsed_seconds(state)
        if state.current_duration and state.current_duration > 0:
            progress = min(100, (elapsed / state.current_duration) * 100)

        state.last_update = time.time()

        payload = {
            "current_track": state.current_track,
            "thumbnail": state.current_thumbnail,
            "query": state.current_query,
            "duration": state.current_duration,
            "elapsed": elapsed,
            "progress": progress,
            "queue": [
                {
                    "title": self._entry_title(entry),
                    "thumbnail": self._entry_thumbnail(entry),
                    "query": entry.query,
                    "source_type": entry.source_type,
                    "pending": entry.player is None,
                }
                for entry in state.queue
            ],
            "is_playing": state.is_playing,
            "is_paused": state.is_paused,
            "error": state.last_error,
            "session_start": state.session_start_time,
            "last_update": state.last_update,
            "history_version": state.history_version,
            "source_type": state.current_history_data.get("source_type") if state.current_history_data else None,
        }

        try:
            save_music_state(guild_id, payload)
        except Exception as exc:
            self.logger.error("Failed to save music state for guild %s: %s", guild_id, exc)

    async def _progress_loop(self, guild_id: int) -> None:
        try:
            while self._get_state(guild_id).is_playing:
                self.update_state(guild_id)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.logger.error("Progress loop error for guild %s: %s", guild_id, exc)

    def add_to_queue(
        self,
        guild_id: int,
        player=None,
        query: Optional[str] = None,
        history_data: Optional[Dict[str, Any]] = None,
        source_type: str = "youtube",
    ) -> QueueEntry:
        state = self._get_state(guild_id)
        entry = QueueEntry(
            player=player,
            query=query,
            history_data=history_data,
            source_type=source_type,
        )
        state.queue.append(entry)
        self.update_state(guild_id)
        return entry

    async def _resolve_queue_entry(self, entry: QueueEntry) -> QueueEntry:
        if entry.player:
            return entry

        if not entry.query:
            raise RuntimeError("Queued track is missing a query.")

        from services.music.source import LocalFileSource, YTDLSource

        if entry.source_type == "local":
            player = await LocalFileSource.from_path(entry.query, loop=self.bot.loop)
            resolved_url = entry.query
            resolved_title = os.path.basename(entry.query)
            resolved_thumbnail = None
        else:
            player = await YTDLSource.from_url(entry.query, loop=self.bot.loop, stream=True)
            resolved_url = getattr(player, "webpage_url", None) or player.url
            resolved_title = getattr(player, "title", None) or entry.query
            resolved_thumbnail = player.thumbnail

        history_data = dict(entry.history_data or {})
        history_data["song_title"] = resolved_title
        history_data["song_url"] = resolved_url
        history_data["thumbnail_url"] = resolved_thumbnail
        if "song_query" not in history_data or not history_data["song_query"]:
            history_data["song_query"] = entry.query

        duration_seconds = None
        try:
            if getattr(player, "duration", None) is not None:
                duration_seconds = max(0, int(round(float(player.duration))))
        except (TypeError, ValueError):
            duration_seconds = None
        history_data["song_duration"] = duration_seconds

        return QueueEntry(
            player=player,
            query=entry.query,
            history_data=history_data,
            source_type=entry.source_type,
        )

    async def _after_song(self, guild_id: int, ctx, voice_client, err) -> None:
        state = self._get_state(guild_id)

        if err:
            self.logger.error("Player error in guild %s: %s", guild_id, err)

        self._cancel_task(state.progress_task)
        state.progress_task = None

        history_to_record = state.current_history_data
        self._reset_current_track(state)

        if history_to_record:
            await self._record_history_async(history_to_record)

        if voice_client and voice_client.is_connected():
            if state.forced_next_entry:
                forced_entry = state.forced_next_entry
                state.forced_next_entry = None
                try:
                    resolved_entry = await self._resolve_queue_entry(forced_entry)
                except Exception as exc:
                    self.logger.error("Failed to resolve forced track in guild %s: %s", guild_id, exc)
                    self.set_error(guild_id, f"Failed to load selected track: {exc}")
                else:
                    self.play_song(
                        guild_id,
                        ctx,
                        voice_client,
                        resolved_entry.player,
                        query=resolved_entry.query,
                        history_data=resolved_entry.history_data,
                    )
                    if ctx:
                        await ctx.send(f"Now playing: **{self._entry_title(resolved_entry)}**")
                    self.update_state(guild_id)
                    return

            while state.queue:
                next_entry = state.queue.pop(0)
                try:
                    resolved_entry = await self._resolve_queue_entry(next_entry)
                except Exception as exc:
                    self.logger.error("Failed to resolve queued track in guild %s: %s", guild_id, exc)
                    self.set_error(guild_id, f"Failed to load queued track: {exc}")
                    continue

                self.play_song(
                    guild_id,
                    ctx,
                    voice_client,
                    resolved_entry.player,
                    query=resolved_entry.query,
                    history_data=resolved_entry.history_data,
                )
                if ctx:
                    await ctx.send(f"Now playing: **{self._entry_title(resolved_entry)}**")
                break

        self.update_state(guild_id)

    def play_song(
        self,
        guild_id: int,
        ctx,
        voice_client,
        player,
        query: Optional[str] = None,
        history_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        state = self._get_state(guild_id)
        if not voice_client or not voice_client.is_connected():
            self.set_error(guild_id, "Bot is not connected to a voice channel.")
            return False
        if player is None:
            self.set_error(guild_id, "Playback failed: no audio source available.")
            return False

        if not state.session_start_time:
            state.session_start_time = time.time()
        self.cancel_idle_timer(guild_id)

        state.current_track = getattr(player, "title", None) or query or "Unknown track"
        state.current_thumbnail = getattr(player, "thumbnail", None)
        song_url = None
        if history_data:
            song_url = history_data.get("song_url")
        if not song_url:
            song_url = getattr(player, "webpage_url", None) or getattr(player, "url", None) or query
        state.current_query = song_url
        state.current_duration = getattr(player, "duration", None)
        state.current_start_time = datetime.datetime.utcnow()
        state.current_elapsed = 0
        state.current_history_data = history_data
        state.is_playing = True
        state.is_paused = False
        self.update_state(guild_id)

        self._cancel_task(state.progress_task)
        state.progress_task = self.bot.loop.create_task(self._progress_loop(guild_id))

        def after_playing(err):
            asyncio.run_coroutine_threadsafe(
                self._after_song(guild_id, ctx, voice_client, err),
                self.bot.loop,
            )

        try:
            voice_client.play(player, after=after_playing)
        except Exception as exc:
            self._cancel_task(state.progress_task)
            state.progress_task = None
            self._reset_current_track(state)
            self.update_state(guild_id)
            self.set_error(guild_id, f"Playback failed: {exc}")
            return False

        return True

    def stop(self, guild_id: int, voice_client) -> None:
        state = self._get_state(guild_id)
        state.queue.clear()
        state.forced_next_entry = None
        self._cancel_task(state.progress_task)
        state.progress_task = None

        history_to_record = state.current_history_data
        self._reset_current_track(state)

        if self._is_voice_active(voice_client):
            voice_client.stop()

        if history_to_record:
            self.bot.loop.create_task(self._record_history_async(history_to_record))

        self.update_state(guild_id)

    def skip(self, guild_id: int, voice_client) -> None:
        if self._is_voice_active(voice_client):
            voice_client.stop()

    def pause(self, guild_id: int, voice_client) -> bool:
        state = self._get_state(guild_id)
        if not voice_client or not voice_client.is_playing():
            return False

        try:
            voice_client.pause()
        except Exception as exc:
            self.set_error(guild_id, f"Pause failed: {exc}")
            return False

        state.current_elapsed = self._get_elapsed_seconds(state)
        state.current_start_time = None
        state.is_playing = False
        state.is_paused = True

        self._cancel_task(state.progress_task)
        state.progress_task = None
        self.update_state(guild_id)
        return True

    def resume(self, guild_id: int, voice_client) -> bool:
        state = self._get_state(guild_id)
        if not voice_client or not voice_client.is_paused():
            return False

        try:
            voice_client.resume()
        except Exception as exc:
            self.set_error(guild_id, f"Resume failed: {exc}")
            return False

        state.current_start_time = datetime.datetime.utcnow()
        state.is_playing = True
        state.is_paused = False

        self._cancel_task(state.progress_task)
        state.progress_task = self.bot.loop.create_task(self._progress_loop(guild_id))
        self.update_state(guild_id)
        return True

    def shuffle(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        random.shuffle(state.queue)
        self.update_state(guild_id)

    def clear_queue(self, guild_id: int) -> None:
        state = self._get_state(guild_id)
        state.queue.clear()
        state.forced_next_entry = None
        self.update_state(guild_id)

    def remove_at(self, guild_id: int, index: int):
        state = self._get_state(guild_id)
        if index < 0 or index >= len(state.queue):
            return None

        removed = state.queue.pop(index)
        self.update_state(guild_id)
        return removed

    def promote_at(self, guild_id: int, index: int):
        state = self._get_state(guild_id)
        if index < 0 or index >= len(state.queue):
            return None

        promoted = state.queue.pop(index)
        state.queue.insert(0, promoted)
        self.update_state(guild_id)
        return promoted

    def move_at(self, guild_id: int, from_index: int, to_index: int) -> bool:
        state = self._get_state(guild_id)
        queue_len = len(state.queue)
        if queue_len == 0:
            return False
        if from_index < 0 or to_index < 0:
            return False
        if from_index >= queue_len or to_index >= queue_len:
            return False
        if from_index == to_index:
            return True

        entry = state.queue.pop(from_index)
        state.queue.insert(to_index, entry)
        self.update_state(guild_id)
        return True

    async def skip_to(self, guild_id: int, voice_client, index: int) -> bool:
        state = self._get_state(guild_id)
        if index < 0 or index >= len(state.queue):
            return False

        queued_track = state.queue.pop(index)

        if self._is_voice_active(voice_client):
            if state.forced_next_entry:
                state.queue.insert(0, state.forced_next_entry)
            state.forced_next_entry = queued_track
            self.update_state(guild_id)
            voice_client.stop()
            return True

        try:
            queued_track = await self._resolve_queue_entry(queued_track)
        except Exception as exc:
            self.set_error(guild_id, f"Failed to load selected track: {exc}")
            self.update_state(guild_id)
            return False

        self.update_state(guild_id)
        return self.play_song(
            guild_id,
            None,
            voice_client,
            queued_track.player,
            query=queued_track.query,
            history_data=queued_track.history_data,
        )
