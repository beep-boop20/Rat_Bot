import asyncio
import datetime
import logging

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from config import DISCORD_TOKEN
from database import SystemStatus, db_manager, init_db
from server_manager import server_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("bot_service")
logging.getLogger("discord.player").setLevel(logging.WARNING)

intents = discord.Intents.default()


class TheRatBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for server in server_manager.get_all_servers():
            logger.info("Initializing DB for server %s", server["id"])
            await init_db(server["id"])

        try:
            await self.load_extension("cogs.music")
            logger.info("Music extension loaded.")
        except Exception as exc:
            logger.error("Failed to load music extension: %s", exc)

        self.heartbeat_task.start()

    @tasks.loop(seconds=30)
    async def heartbeat_task(self):
        for server in server_manager.get_all_servers():
            try:
                async with db_manager.get_session(server["id"])() as session:
                    now = datetime.datetime.utcnow()
                    result = await session.execute(
                        select(SystemStatus).where(SystemStatus.key == "heartbeat")
                    )
                    status = result.scalar_one_or_none()

                    if not status:
                        status = SystemStatus(key="heartbeat", value="online")
                        session.add(status)

                    status.timestamp = now
                    status.value = "online"
                    await session.commit()
            except Exception as exc:
                logger.error("Heartbeat failed for server %s: %s", server["id"], exc)

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)

        for guild in self.guilds:
            server_manager.add_server(
                guild.id,
                guild.name,
                str(guild.icon.url) if guild.icon else None,
            )
            await init_db(guild.id)
            logger.info("Synchronized guild metadata: %s (%s)", guild.name, guild.id)

        try:
            synced = await self.tree.sync()
            logger.info("Synced %s slash commands globally", len(synced))
        except Exception as exc:
            logger.error("Failed to sync commands: %s", exc)

    async def on_guild_join(self, guild: discord.Guild):
        server_manager.add_server(
            guild.id,
            guild.name,
            str(guild.icon.url) if guild.icon else None,
        )
        await init_db(guild.id)
        logger.info("Joined guild: %s (%s)", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild):
        server_manager.remove_server(guild.id)
        await db_manager.dispose_engine(guild.id)
        logger.info("Removed guild: %s (%s)", guild.name, guild.id)


async def main():
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not found. Please configure it in settings or .env.")
        return

    bot = TheRatBot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Fatal error: %s", exc)
