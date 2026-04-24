from __future__ import annotations

import copy
import queue
from typing import Any, Dict, Iterator

_COMMAND_QUEUE = None
_STATE_STORE = None


def configure_music_ipc(command_queue, state_store) -> None:
    """Attach shared music IPC primitives for the current process."""
    global _COMMAND_QUEUE, _STATE_STORE
    _COMMAND_QUEUE = command_queue
    _STATE_STORE = state_store


def _require_command_queue():
    if _COMMAND_QUEUE is None:
        raise RuntimeError("Music IPC is not configured: command queue is missing.")
    return _COMMAND_QUEUE


def _require_state_store():
    if _STATE_STORE is None:
        raise RuntimeError("Music IPC is not configured: state store is missing.")
    return _STATE_STORE


def default_music_state() -> Dict[str, Any]:
    return {
        "current_track": None,
        "thumbnail": None,
        "query": None,
        "duration": None,
        "elapsed": 0,
        "progress": 0,
        "queue": [],
        "is_playing": False,
        "is_paused": False,
        "error": None,
        "session_start": None,
        "last_update": 0,
        "history_version": 0,
        "source_type": None,
    }


def load_music_state(guild_id: int) -> Dict[str, Any]:
    state_store = _require_state_store()
    state = state_store.get(int(guild_id))
    merged = default_music_state()
    if isinstance(state, dict):
        merged.update(copy.deepcopy(state))
    return merged


def save_music_state(guild_id: int, state: Dict[str, Any]) -> None:
    state_store = _require_state_store()
    # Store a detached copy so future mutations in caller do not affect shared state.
    state_store[int(guild_id)] = copy.deepcopy(state)


def enqueue_music_command(command: Dict[str, Any]) -> None:
    command_queue = _require_command_queue()
    command_queue.put(copy.deepcopy(command))


def iter_music_commands() -> Iterator[Dict[str, Any]]:
    command_queue = _require_command_queue()
    while True:
        try:
            command = command_queue.get_nowait()
        except queue.Empty:
            break

        if isinstance(command, dict):
            yield command
