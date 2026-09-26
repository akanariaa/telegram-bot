import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

B2_KEY_ID = os.getenv("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "")
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "archive")

USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
PROXY_PROTOCOL = os.getenv("PROXY_PROTOCOL", "socks5")
PROXY_HOST_PORT = os.getenv("PROXY_HOST_PORT", "")
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")
DOCKER_PROXY_CONTAINER = os.getenv("DOCKER_PROXY_CONTAINER", "")

ALLOWED_USER_IDS: list[int] = [
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
]

DOWNLOAD_BASE_DIR = os.getenv("DOWNLOAD_BASE_DIR", "./data/downloads")
DB_PATH = os.getenv("DB_PATH", "./data/bot.db")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
PREFERRED_RESOLUTIONS = ["720p", "480p", "360p"]


def get_proxy_dict() -> dict | None:
    if not USE_PROXY or not PROXY_HOST_PORT:
        return None
    auth = ""
    if PROXY_USERNAME and PROXY_PASSWORD:
        auth = f"{PROXY_USERNAME}:{PROXY_PASSWORD}@"
    url = f"{PROXY_PROTOCOL}://{auth}{PROXY_HOST_PORT}"
    return {"http": url, "https": url}