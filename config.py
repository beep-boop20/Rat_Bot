import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
KLIPY_API_KEY = os.getenv('KLIPY_API_KEY') or os.getenv('TENOR_API_KEY', '')
