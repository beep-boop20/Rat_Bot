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

## License

MIT
