import os
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"


def load_env(*, override: bool = False) -> None:
    load_dotenv(dotenv_path=ENV_FILE_PATH, override=override)


load_env()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
KLIPY_API_KEY = os.getenv('KLIPY_API_KEY') or os.getenv('TENOR_API_KEY', '')
