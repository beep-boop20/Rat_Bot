from sqlalchemy import BigInteger, Column, DateTime, Integer, String, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime

from paths import resolve_storage_path

Base = declarative_base()


class MusicHistory(Base):
    __tablename__ = 'music_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger)
    username = Column(String)
    user_avatar_url = Column(String, nullable=True)
    song_title = Column(String)
    song_url = Column(String, nullable=True)
    song_query = Column(String, nullable=True)
    song_duration = Column(Integer, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    source_type = Column(String)  # 'youtube', 'local', etc.


class SystemStatus(Base):
    __tablename__ = 'system_status'
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True)
    value = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


class DatabaseManager:
    def __init__(self):
        self._engines = {}
        self._sessionmakers = {}

    def get_engine(self, guild_id: int):
        if guild_id not in self._engines:
            from server_manager import server_manager
            db_path = resolve_storage_path(server_manager.get_db_path(guild_id))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"
            self._engines[guild_id] = create_async_engine(url, echo=False)
        return self._engines[guild_id]

    def get_session(self, guild_id: int):
        if guild_id not in self._sessionmakers:
            engine = self.get_engine(guild_id)
            self._sessionmakers[guild_id] = sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
        return self._sessionmakers[guild_id]

    async def dispose_engine(self, guild_id: int):
        if guild_id in self._engines:
            await self._engines[guild_id].dispose()
            del self._engines[guild_id]
        if guild_id in self._sessionmakers:
            del self._sessionmakers[guild_id]


db_manager = DatabaseManager()


async def init_db(guild_id: int):
    engine = db_manager.get_engine(guild_id)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_migrations)


def _run_migrations(sync_conn) -> None:
    inspector = inspect(sync_conn)
    if "music_history" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("music_history")}
    if "song_duration" not in columns:
        sync_conn.execute(text("ALTER TABLE music_history ADD COLUMN song_duration INTEGER"))
