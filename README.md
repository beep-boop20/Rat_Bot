# TheRatBot 🐀🎵

A Discord music bot with a web dashboard. Play music from YouTube in your voice channels, manage the queue, and control everything from a clean web interface.

## Features

- **Music Playback** — Play songs from YouTube via search or URL
- **Queue Management** — Add, skip, shuffle, and loop tracks
- **Web Dashboard** — Control music and view history from your browser
- **Multi-Server** — Supports multiple Discord servers
- **File Upload** — Upload and play local audio files

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/your-username/TheRatBot.git
   cd TheRatBot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the bot**:
   - Copy `.env.example` to `.env`
   - Add your Discord bot token (from the [Discord Developer Portal](https://discord.com/developers/applications))
   - Or configure it via the web dashboard after starting

4. **Run**:
   ```bash
   python main.py
   ```

5. **Open the dashboard**: [http://localhost:7734](http://localhost:7734)

## Docker Deployment (Homeserver)

1. **Prepare persistent data and runtime env**:
   ```bash
   mkdir -p data
   cp .env.example .env
   ```
   Then edit `.env` and set:
   - `DASHBOARD_BIND_IP` to your Alpine VM LAN IP (for example `192.168.1.50`)
   - `DISCORD_TOKEN`
   - `KLIPY_API_KEY`
   If you already have local data in project root, move it into `data/` first (`servers.json`, `ratbot_*.db`, `bot.log`, and `temp/` if needed).

2. **Build and start**:
   ```bash
   docker compose up -d --build
   ```

3. **Check health/logs**:
   ```bash
   docker compose ps
   docker compose logs -f
   ```

4. **Open dashboard**: `http://<ALPINE_VM_IP>:7734`
   The compose file now requires `DASHBOARD_BIND_IP` and binds only to that address.

### Safe Updates

```bash
git pull
docker compose up -d --build
```

### Backup / Restore

Backup:
```bash
tar -czf ratbot-data-backup.tar.gz data
```

Restore:
```bash
tar -xzf ratbot-data-backup.tar.gz
docker compose up -d --build
```

## Discord Bot Setup

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications)
2. Go to **Bot** → enable **Message Content Intent**
3. Go to **OAuth2** → **URL Generator** → select `bot` + `applications.commands`
4. Select permissions: `Connect`, `Speak`, `Send Messages`, `Use Slash Commands`
5. Use the generated URL to invite the bot to your server

## Slash Commands

| Command | Description |
|---------|-------------|
| `/play <query>` | Play a song from YouTube |
| `/skip` | Skip the current song |
| `/queue` | Show the current queue |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/stop` | Stop and clear the queue |
| `/leave` | Disconnect from voice |

## Requirements

- Python 3.10+
- FFmpeg (must be in PATH)
- Discord bot token

## Recent Changes

- Replaced file-based music IPC with in-memory IPC (`multiprocessing.Queue` + shared state dict).
- Removed restart/shutdown flag files in favor of queue-based supervisor control.
- Switched restart flow to a non-recursive supervisor loop for cleaner process lifecycle handling.
- Removed runtime Discord voice version/encryption checks from bot startup; dependency version is enforced by `requirements.txt`.
- Added `RATBOT_DATA_DIR` storage routing so `.env`, `servers.json`, DB files, logs, and uploads can persist in one mounted directory.
- Added Docker deployment artifacts (`Dockerfile`, `docker-compose.yml`, `.dockerignore`) with healthcheck and hardened container defaults.

## License

MIT
