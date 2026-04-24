import os
from dotenv import load_dotenv

from paths import env_file_path

load_dotenv(dotenv_path=env_file_path())

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
KLIPY_API_KEY = os.getenv('KLIPY_API_KEY') or os.getenv('TENOR_API_KEY', '')
