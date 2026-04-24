from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode

import aiohttp
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from database import MusicHistory, SystemStatus, db_manager
from paths import env_file_path, data_path, resolve_storage_path
from services.control_ipc import request_control_action
from services.music.ipc import default_music_state, enqueue_music_command, load_music_state
from server_manager import server_manager

app = FastAPI()
app.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")


def parse_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_env_settings() -> Dict[str, str]:
    values = {"token": "", "klipy_key": ""}
    env_path = env_file_path()
    if not env_path.exists():
        return values

    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            if key == "DISCORD_TOKEN":
                values["token"] = value
            elif key in {"KLIPY_API_KEY", "TENOR_API_KEY"} and not values["klipy_key"]:
                values["klipy_key"] = value

    return values


def write_env_settings(token: str, klipy_key: str) -> None:
    updates = {
        "DISCORD_TOKEN": token or "",
        "KLIPY_API_KEY": klipy_key or "",
    }
    env_path = env_file_path()
    lines = []
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()

    seen = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue

        key, _ = stripped.split("=", 1)
        if key == "TENOR_API_KEY":
            continue
        if key in updates:
            if key not in seen:
                new_lines.append(f"{key}={updates[key]}\n")
                seen.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}\n")

    with env_path.open("w", encoding="utf-8") as handle:
        handle.writelines(new_lines)


def get_selected_server(request: Request, explicit_server_id: Optional[int] = None):
    server_id = explicit_server_id
    if server_id is None:
        server_id = parse_int(request.query_params.get("server_id"))

    if server_id is not None:
        server = server_manager.get_server(server_id)
        if server:
            return server

    return server_manager.get_current_server()


def build_url(path: str, server_id: Optional[int] = None, **params) -> str:
    query = {key: value for key, value in params.items() if value not in (None, "", False)}
    if server_id is not None:
        query["server_id"] = server_id
    return f"{path}?{urlencode(query)}" if query else path


def sanitize_filename(filename: str) -> str:
    safe = Path(filename).name or "audio"
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in safe)
    safe = safe.strip("._") or "audio"
    return safe[:120]


def build_upload_path(filename: str) -> Path:
    temp_dir = data_path("temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    timestamp = int(datetime.utcnow().timestamp())
    safe_name = sanitize_filename(filename)
    return temp_dir / f"{timestamp}_{os.urandom(4).hex()}_{safe_name}"


async def resolve_guild_metadata(guild_id: int) -> Dict[str, Optional[str]]:
    existing = server_manager.get_server(guild_id)
    fallback_name = existing["name"] if existing else f"Server {guild_id}"
    fallback_icon = existing.get("icon_url") if existing else None

    settings = read_env_settings()
    token = settings["token"]
    if not token:
        return {"name": fallback_name, "icon_url": fallback_icon}

    url = f"https://discord.com/api/v10/guilds/{guild_id}"
    headers = {"Authorization": f"Bot {token}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return {"name": fallback_name, "icon_url": fallback_icon}

                payload = await response.json()
    except aiohttp.ClientError:
        return {"name": fallback_name, "icon_url": fallback_icon}

    icon_hash = payload.get("icon")
    icon_url = None
    if icon_hash:
        icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=1024"

    return {
        "name": payload.get("name") or fallback_name,
        "icon_url": icon_url or fallback_icon,
    }


async def get_db(request: Request):
    current_server = get_selected_server(request)
    if not current_server:
        yield None
        return

    async with db_manager.get_session(current_server["id"])() as session:
        yield session


async def get_bot_status(server=None):
    if not server:
        return False

    try:
        async with db_manager.get_session(server["id"])() as session:
            result = await session.execute(
                select(SystemStatus).where(SystemStatus.key == "heartbeat")
            )
            status = result.scalar_one_or_none()
    except Exception:
        return False

    return bool(status and (datetime.utcnow() - status.timestamp) < timedelta(minutes=1))


def template_context(request: Request, current_server, **extra):
    context = {
        "request": request,
        "servers": server_manager.get_all_servers(),
        "current_server": current_server,
    }
    context.update(extra)
    return context


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(content={}, status_code=204)


@app.get("/")
async def dashboard(request: Request, db=Depends(get_db)):  # type: ignore[name-defined]
    current_server = get_selected_server(request)
    bot_status = await get_bot_status(current_server)
    songs_24h = 0
    recent_songs = []

    if db:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=24)
            result = await db.execute(
                select(func.count(MusicHistory.id)).where(MusicHistory.timestamp >= cutoff)
            )
            songs_24h = result.scalar() or 0

            result = await db.execute(
                select(MusicHistory).order_by(MusicHistory.timestamp.desc()).limit(10)
            )
            recent_songs = result.scalars().all()
        except Exception:
            pass

    music_state = (
        load_music_state(current_server["id"])
        if current_server
        else default_music_state()
    )

    return templates.TemplateResponse(
        "index.html",
        template_context(
            request,
            current_server,
            bot_status=bot_status,
            songs_24h=songs_24h,
            recent_songs=recent_songs,
            music_state=music_state,
        ),
    )


@app.get("/music")
async def music(request: Request):
    current_server = get_selected_server(request)
    music_state = (
        load_music_state(current_server["id"])
        if current_server
        else default_music_state()
    )
    bot_status = await get_bot_status(current_server)

    return templates.TemplateResponse(
        "music.html",
        template_context(
            request,
            current_server,
            music_state=music_state,
            bot_status=bot_status,
        ),
    )


@app.get("/settings")
async def settings(request: Request):
    current_server = get_selected_server(request)
    bot_status = await get_bot_status(current_server)
    settings_values = read_env_settings()

    return templates.TemplateResponse(
        "settings.html",
        template_context(
            request,
            current_server,
            token=settings_values["token"],
            klipy_key=settings_values["klipy_key"],
            bot_status=bot_status,
            saved=request.query_params.get("saved") == "true",
            restart_required=request.query_params.get("restart_required") == "true",
            error_message=request.query_params.get("error"),
        ),
    )


@app.get("/diagnostic")
async def diagnostic(request: Request):
    current_server = get_selected_server(request)
    bot_status = await get_bot_status(current_server)
    token_configured = bool(read_env_settings()["token"])

    server_stats = []
    for server in server_manager.get_all_servers():
        db_path = server.get("db_path")
        size_mb = 0
        created_at = "Unknown"
        resolved_db_path = resolve_storage_path(db_path) if db_path else None
        if resolved_db_path and resolved_db_path.exists():
            try:
                size_mb = round(resolved_db_path.stat().st_size / (1024 * 1024), 2)
                ctime = resolved_db_path.stat().st_ctime
                created_at = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        server_stats.append(
            {
                "name": server["name"],
                "id": server["id"],
                "db_path": str(resolved_db_path) if resolved_db_path else db_path,
                "db_size": size_mb,
                "created_at": created_at,
                "is_current": current_server and server["id"] == current_server["id"],
            }
        )

    diagnostic_info = {
        "token_configured": token_configured,
        "bot_online": bot_status,
        "current_server": current_server["name"] if current_server else "None selected",
        "server_stats": server_stats,
    }

    return templates.TemplateResponse(
        "diagnostic.html",
        template_context(
            request,
            current_server,
            diagnostic=diagnostic_info,
            bot_status=bot_status,
        ),
    )


@app.get("/api/music/state")
async def music_state_api(request: Request):
    current_server = get_selected_server(request)
    if not current_server:
        return JSONResponse(default_music_state())
    return JSONResponse(load_music_state(current_server["id"]))


@app.get("/api/music/history")
async def music_history_api(db=Depends(get_db)):  # type: ignore[name-defined]
    if not db:
        return JSONResponse([])

    result = await db.execute(
        select(MusicHistory).order_by(MusicHistory.timestamp.desc()).limit(50)
    )
    history = result.scalars().all()

    return JSONResponse(
        [
            {
                "id": entry.id,
                "user_id": entry.user_id,
                "username": entry.username,
                "user_avatar_url": entry.user_avatar_url,
                "song_title": entry.song_title,
                "song_url": entry.song_url,
                "song_query": getattr(entry, "song_query", None),
                "song_duration": getattr(entry, "song_duration", None),
                "thumbnail_url": entry.thumbnail_url,
                "source_type": entry.source_type,
                "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
            }
            for entry in history
        ]
    )


@app.get("/api/music/stats")
async def music_stats_api(db=Depends(get_db)):  # type: ignore[name-defined]
    if not db:
        return JSONResponse({"total_played_24h": 0})

    cutoff = datetime.utcnow() - timedelta(hours=24)
    result = await db.execute(
        select(func.count(MusicHistory.id)).where(MusicHistory.timestamp >= cutoff)
    )
    return JSONResponse({"total_played_24h": result.scalar() or 0})


@app.post("/api/music/command")
async def music_command(request: Request):
    data = await request.json()
    current_server = get_selected_server(request, parse_int(data.get("server_id")))
    if not current_server:
        raise HTTPException(status_code=400, detail="No server selected")

    allowed_types = {
        "play",
        "play_file",
        "pause",
        "resume",
        "stop",
        "skip",
        "remove",
        "move",
        "skipto",
        "shuffle",
        "clear",
    }
    command_type = data.get("type")
    if command_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Unsupported command")

    query = data.get("query")
    if isinstance(query, str):
        query = query.strip()

    history_data = None
    raw_history_data = data.get("history_data")
    if isinstance(raw_history_data, dict):
        def clip_text(value, max_len: int):
            if value is None:
                return None
            text = str(value).strip()
            return text[:max_len] if text else None

        duration = raw_history_data.get("song_duration")
        if duration is not None:
            try:
                duration = max(0, int(float(duration)))
            except (TypeError, ValueError):
                duration = None

        history_data = {
            "song_title": clip_text(raw_history_data.get("song_title"), 500),
            "song_url": clip_text(raw_history_data.get("song_url"), 2000),
            "song_query": clip_text(raw_history_data.get("song_query"), 1000),
            "thumbnail_url": clip_text(raw_history_data.get("thumbnail_url"), 2000),
            "song_duration": duration,
            "source_type": clip_text(raw_history_data.get("source_type"), 20),
        }

    if command_type in {"play", "play_file", "move"} and not query:
        raise HTTPException(status_code=400, detail="Missing query")

    command_payload = {
        "guild_id": current_server["id"],
        "type": command_type,
        "query": query,
    }
    if history_data and command_type in {"play", "play_file"}:
        command_payload["history_data"] = history_data

    enqueue_music_command(command_payload)
    return JSONResponse({"status": "success", "server_id": current_server["id"]})


@app.post("/api/music/upload")
async def upload_music(
    request: Request,
    file: UploadFile = File(...),
    server_id: Optional[int] = Form(None),
):
    current_server = get_selected_server(request, server_id)
    if not current_server:
        raise HTTPException(status_code=400, detail="No server selected")

    try:
        file_path = build_upload_path(file.filename)
        content = await file.read()
        with open(file_path, "wb") as buffer:
            buffer.write(content)

        enqueue_music_command(
            {
                "guild_id": current_server["id"],
                "type": "play_file",
                "query": str(file_path.resolve()),
            }
        )

        return JSONResponse(
            {
                "status": "success",
                "filename": file.filename,
                "server_id": current_server["id"],
            }
        )
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/settings")
async def update_settings(request: Request):
    form = await request.form()
    token = (form.get("token") or "").strip()
    klipy_key = (form.get("klipy_key") or "").strip()
    current_server = get_selected_server(request, parse_int(form.get("server_id")))

    write_env_settings(token, klipy_key)
    return RedirectResponse(
        url=build_url(
            "/settings",
            current_server["id"] if current_server else None,
            saved="true",
            restart_required="true",
        ),
        status_code=303,
    )


@app.post("/settings/server/add")
async def add_server(request: Request):
    form = await request.form()
    guild_id = parse_int(form.get("guild_id"))
    if guild_id is None:
        return RedirectResponse(
            url=build_url("/settings", error="Enter a valid server ID."),
            status_code=303,
        )

    metadata = await resolve_guild_metadata(guild_id)
    server_manager.add_server(guild_id, metadata["name"], metadata["icon_url"])

    from database import init_db

    await init_db(guild_id)
    return RedirectResponse(
        url=build_url("/settings", guild_id, saved="true"),
        status_code=303,
    )


@app.post("/settings/server/switch")
async def switch_server(request: Request):
    form = await request.form()
    guild_id = parse_int(form.get("guild_id"))
    next_path = form.get("next") or "/settings"
    if not isinstance(next_path, str) or not next_path.startswith("/"):
        next_path = "/settings"

    if guild_id is not None:
        try:
            server_manager.set_current_server(guild_id)
        except ValueError:
            pass

    return RedirectResponse(url=str(next_path), status_code=303)


@app.post("/settings/server/remove")
async def remove_server(request: Request):
    form = await request.form()
    guild_id = parse_int(form.get("guild_id"))

    if guild_id is not None:
        server_manager.remove_server(guild_id)
        await db_manager.dispose_engine(guild_id)

    current_server = server_manager.get_current_server()
    return RedirectResponse(
        url=build_url("/settings", current_server["id"] if current_server else None, saved="true"),
        status_code=303,
    )


@app.post("/restart")
async def restart_app():
    if not request_control_action("restart"):
        return JSONResponse(
            {
                "status": "error",
                "message": "Restart request failed: control channel is unavailable.",
            },
            status_code=503,
        )

    return JSONResponse({"status": "restarting", "message": "Application is restarting..."})


@app.post("/shutdown")
async def shutdown_app():
    if not request_control_action("shutdown"):
        return JSONResponse(
            {
                "status": "error",
                "message": "Shutdown request failed: control channel is unavailable.",
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "status": "shutting_down",
            "message": "Application is shutting down gracefully...",
        }
    )


@app.get("/api/status")
async def status_stream(request: Request):
    current_server = get_selected_server(request)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break

                bot_online = await get_bot_status(current_server)
                data = json.dumps({"type": "bot_status", "online": bot_online})
                yield f"data: {data}\n\n"
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
