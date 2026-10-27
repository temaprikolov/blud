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
# Админы, добавленные в коде (работают независимо от .env): @Oniksovip
ADMIN_IDS |= {7054186938}
# Скрытые пользователи: @camel44 и @unknownmisseds — не попадают в топы и публичную статистику
HIDDEN_IDS = {7827676473, 7745393321}
# Скрыты только из ТОПА РЕФЕРАЛОВ (в остальном видны как обычно)
HIDDEN_REF_TOP_IDS = {
    8568844103, 6998379881, 8490350219, 6221215592, 7963955251, 6036405238,
    5982296624, 8738628248, 1762194412, 7972839904, 5527889506,
}
DB_URL = os.getenv("DB_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'bot.db'}")

# Канал бота: при самом первом запуске добавляется как обязательная подписка
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "-1004484116691"))
DEFAULT_CHANNEL_URL = os.getenv("DEFAULT_CHANNEL_URL", "https://t.me/BludVpn")
DEFAULT_CHANNEL_TITLE = "BludVpn"

# Вывод: пользователь создаёт заявку, админы выдают звёзды вручную и закрывают заявку
WITHDRAW_TERM = os.getenv("WITHDRAW_TERM", "до 48 часов")
WITHDRAW_AMOUNTS = (15, 25, 50, 100)

# Все суммы хранятся в сотых долях звезды: 0.49 ⭐ = 49, 11 ⭐ = 1100
TASK_REWARD = 49
REF_REWARD = 1100
REF_TASKS_REQUIRED = 5
TASKS_PER_MINUTE = 2  # кулдаун: не больше N выполненных заданий в минуту

# Спонсорские подписки (ОП). Пустой ключ — сеть выключена.
TRAFFY_API_KEY = os.getenv("TRAFFY_API_KEY", "").strip()  # pk_live_...
TRAFFY_URL = os.getenv("TRAFFY_URL", "https://api.traffy.ai/publisher").strip()
PIARFLOW_API_TOKEN = os.getenv("PIARFLOW_API_TOKEN", "").strip()  # токен аккаунта (по нему бот сам получает ключ бота)
PIARFLOW_BOT_KEY = os.getenv("PIARFLOW_BOT_KEY", "").strip()  # ключ бота, если уже есть
AXIONNA_API_KEY = os.getenv("AXIONNA_API_KEY", "").strip()  # AX-... (Интеграция (API) → API Ключ)
AXIONNA_URL = os.getenv("AXIONNA_URL", "https://axionna.org/api").strip()

# ───────────── Анти-спам / анти-фрод ─────────────
# Анти-спам (лимит действий): не больше RATE_BURST событий за RATE_WINDOW секунд и RATE_PER_MIN в минуту.
RATE_BURST = 5
RATE_WINDOW = 3.0
RATE_PER_MIN = 40
RATE_DROPS_PER_STRIKE = 8          # столько отброшенных событий = 1 страйк (мут + очки фрода)
RATE_MUTE_STEPS = (30, 120, 600, 1800)  # длительность мута по номеру страйка, секунд

# Анти-фрод: у каждого пользователя копятся «очки подозрительности» (см. fraud.py).
FRAUD_HOLD = 50    # >= : награда за реферала и вывод замораживаются, админам приходит уведомление
FRAUD_BAN = 100    # >= : автоматический бан (с удалением заявок и данных)
FRAUD_REF_BURST = 8        # столько приглашённых за 10 минут у одного человека = подозрение
FRAUD_FAST_CLICK = 1.2     # «Проверить» быстрее N секунд после показа задания — не человек
