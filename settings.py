"""Настройки, редактируемые из админки. Хранятся в БД, читаются из кэша в памяти."""
from __future__ import annotations

from sqlalchemy import select

import config
from db import SessionLocal, Setting

DEFAULTS: dict[str, str] = {
    "task_reward": str(config.TASK_REWARD),  # сотые доли звезды
    "ref_reward": str(config.REF_REWARD),
    "ref_tasks": str(config.REF_TASKS_REQUIRED),
    "withdraw_amounts": ",".join(str(a) for a in config.WITHDRAW_AMOUNTS),
    "withdraw_term": config.WITHDRAW_TERM,
    "task_rate": str(config.TASKS_PER_MINUTE),
}

_cache: dict[str, str] = dict(DEFAULTS)


async def load() -> None:
    async with SessionLocal() as s:
        for row in (await s.execute(select(Setting))).scalars():
            if row.key in DEFAULTS:
                _cache[row.key] = row.value


async def set_value(key: str, value: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()
    _cache[key] = value


async def reset(key: str) -> None:
    await set_value(key, DEFAULTS[key])


def task_reward() -> int:
    return int(_cache["task_reward"])


def ref_reward() -> int:
    return int(_cache["ref_reward"])


def ref_tasks() -> int:
    return int(_cache["ref_tasks"])


def task_rate() -> int:
    return int(_cache["task_rate"])


def withdraw_amounts() -> tuple[int, ...]:
    return tuple(int(x) for x in _cache["withdraw_amounts"].split(",") if x.strip())


def withdraw_term() -> str:
    return _cache["withdraw_term"]

