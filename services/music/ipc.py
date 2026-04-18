from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

TEMP_DIR = Path("temp")
COMMANDS_DIR = TEMP_DIR / "commands"
STATES_DIR = TEMP_DIR / "states"
COMMAND_TTL_SECONDS = 10 * 60


def ensure_music_dirs() -> None:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    STATES_DIR.mkdir(parents=True, exist_ok=True)


def get_state_path(guild_id: int) -> Path:
    ensure_music_dirs()
    return STATES_DIR / f"music_state_{guild_id}.json"


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
    path = get_state_path(guild_id)
    if not path.exists():
        return default_music_state()

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default_music_state()

    state = default_music_state()
    state.update(data)
    return state


def save_music_state(guild_id: int, state: Dict[str, Any]) -> None:
    path = get_state_path(guild_id)
    payload = json.dumps(state, ensure_ascii=False)

    # On Windows, atomic replace may fail while another process is reading the
    # destination file. Retry replace first, then fall back to direct write.
    for attempt in range(3):
        temp_path = path.with_suffix(f".{time.time_ns()}_{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    for attempt in range(3):
        try:
            path.write_text(payload, encoding="utf-8")
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))

    raise PermissionError(f"Unable to save music state for guild {guild_id}")


def enqueue_music_command(command: Dict[str, Any]) -> None:
    ensure_music_dirs()
    command_path = COMMANDS_DIR / f"{time.time_ns()}_{uuid.uuid4().hex}.json"
    command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")


def _is_stale_command(command_path: Path, now: float) -> bool:
    try:
        return (now - command_path.stat().st_mtime) > COMMAND_TTL_SECONDS
    except OSError:
        return True


def iter_music_commands() -> Iterator[Tuple[Path, Dict[str, Any]]]:
    ensure_music_dirs()
    now = time.time()
    for command_path in sorted(COMMANDS_DIR.glob("*.json")):
        if _is_stale_command(command_path, now):
            delete_music_command(command_path)
            continue

        try:
            with command_path.open("r", encoding="utf-8") as handle:
                command = json.load(handle)
        except (OSError, json.JSONDecodeError):
            delete_music_command(command_path)
            continue

        yield command_path, command


def delete_music_command(command_path: Path) -> None:
    try:
        command_path.unlink(missing_ok=True)
    except OSError:
        pass
