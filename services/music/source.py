import discord
import yt_dlp
import asyncio
import os

# Suppress noise from youtube_dl and ffmpeg
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ''

ytdl_format_options = {
    # Prefer direct audio formats to avoid HLS jitter/catch-up behavior.
    'format': 'bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

ffmpeg_options = {
    'options': '-vn',
    'before_options': '-nostdin',
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


def _normalize_info(data):
    if not data:
        return None
    if 'entries' in data:
        entries = data.get('entries') or []
        data = next((entry for entry in entries if entry), None)
    return data


def _build_youtube_watch_url(data):
    if not data:
        return None
    video_id = data.get('id')
    extractor = str(data.get('extractor_key') or data.get('ie_key') or '').lower()
    if video_id and 'youtube' in extractor:
        return f"https://www.youtube.com/watch?v={video_id}"
    webpage_url = data.get('webpage_url') or data.get('original_url')
    if webpage_url and ('youtube.com/watch' in webpage_url or 'youtu.be/' in webpage_url):
        return webpage_url
    return None


def _build_thumbnail_url(data):
    if not data:
        return None
    if data.get('thumbnail'):
        return data.get('thumbnail')
    watch_url = _build_youtube_watch_url(data)
    if watch_url:
        video_id = data.get('id')
        if video_id:
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None

class AudioSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title') or "Unknown track"
        self.url = data.get('url')
        self.webpage_url = _build_youtube_watch_url(data) or data.get('webpage_url') or data.get('original_url') or data.get('url')
        self.thumbnail = _build_thumbnail_url(data)
        self.duration = data.get('duration')

class YTDLSource(AudioSource):
    @classmethod
    async def fetch_info(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: _normalize_info(ytdl.extract_info(url, download=False)),
        )
        if not data:
            raise RuntimeError("No playable result found for the provided query.")

        return {
            'title': data.get('title') or url,
            'url': _build_youtube_watch_url(data) or data.get('webpage_url') or data.get('original_url') or data.get('url') or url,
            'thumbnail': _build_thumbnail_url(data),
            'duration': data.get('duration'),
            'query': url,
        }

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False, start_time=0):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: _normalize_info(ytdl.extract_info(url, download=not stream)),
        )
        if not data:
            raise RuntimeError("No playable result found for the provided query.")

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        
        # Create a copy of options to avoid modifying global state
        opts = ffmpeg_options.copy()
        before_parts = ["-thread_queue_size 1024"]
        if stream:
            before_parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
        if start_time > 0:
            before_parts.append(f"-ss {start_time}")
        if opts.get('before_options'):
            before_parts.append(opts['before_options'])
        opts['before_options'] = " ".join(part for part in before_parts if part).strip()

        return cls(discord.FFmpegPCMAudio(filename, **opts), data=data)

class LocalFileSource(AudioSource):
    @classmethod
    async def from_path(cls, path, *, loop=None, start_time=0):
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")
            
        # Create a dummy data dict for compatibility
        data = {
            'title': os.path.basename(path),
            'url': path,
            'thumbnail': None,
            'duration': None # Duration calculation requires external libs like mutagen
        }
        
        options = {'before_options': '-thread_queue_size 1024 -nostdin'}
        if start_time > 0:
            options['before_options'] = f"-thread_queue_size 1024 -ss {start_time} -nostdin"
        
        return cls(discord.FFmpegPCMAudio(path, **options), data=data)
