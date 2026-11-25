from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "bludstarsbot").strip().lstrip("@")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", "7727571734,7745393321"))
ADMIN_IDS |= {7054186938}
HIDDEN_IDS = {7827676473, 7745393321}
HIDDEN_REF_TOP_IDS = {
    8568844103, 6998379881, 8490350219, 6221215592, 7963955251, 6036405238,
    5982296624, 8738628248, 1762194412, 7972839904, 5527889506,
}
DB_URL = os.getenv("DB_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'bot.db'}")

DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "-1004484116691"))
DEFAULT_CHANNEL_URL = os.getenv("DEFAULT_CHANNEL_URL", "https://t.me/BludVpn")
DEFAULT_CHANNEL_TITLE = "BludVpn"

WITHDRAW_TERM = os.getenv("WITHDRAW_TERM", "до 48 часов")
WITHDRAW_AMOUNTS = (15, 25, 50, 100)

TASK_REWARD = 49
REF_REWARD = 1100
REF_TASKS_REQUIRED = 5
TASKS_PER_MINUTE = 2
OLD_REF_REWARD = 150
OLD_REF_DAYS = 7

TRAFFY_API_KEY = os.getenv("TRAFFY_API_KEY", "").strip()
TRAFFY_URL = os.getenv("TRAFFY_URL", "https://api.traffy.ai/publisher").strip()
PIARFLOW_API_TOKEN = os.getenv("PIARFLOW_API_TOKEN", "").strip()
PIARFLOW_BOT_KEY = os.getenv("PIARFLOW_BOT_KEY", "").strip()
AXIONNA_API_KEY = os.getenv("AXIONNA_API_KEY", "").strip()
AXIONNA_URL = os.getenv("AXIONNA_URL", "https://axionna.org/api").strip()

RATE_BURST = 5
RATE_WINDOW = 3.0
RATE_PER_MIN = 40
RATE_DROPS_PER_STRIKE = 8
RATE_MUTE_STEPS = (30, 120, 600, 1800)

FRAUD_HOLD = 50
FRAUD_BAN = 100
FRAUD_REF_BURST = 8
FRAUD_FAST_CLICK = 1.2
