"""Анти-спам: лимит частоты действий на пользователя (скользящее окно) + мут с нарастающим сроком.

Лишние нажатия молча отбрасываются (до БД и до проверки подписок они не доходят), за повторный флуд
человек получает мут и очки подозрительности в fraud; набрал достаточно — moderation.evaluate его банит.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

import config
import fraud
import moderation


class _S:
    __slots__ = ("hits", "muted_until", "drops", "strikes", "last_seen", "told")

    def __init__(self) -> None:
        self.hits: deque[float] = deque()
        self.muted_until = 0.0
        self.drops = 0
        self.strikes = 0
        self.last_seen = 0.0
        self.told = 0.0  # когда последний раз отвечали «не так быстро»


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._st: dict[int, _S] = {}
        self._gc_at = time.time() + 600

    def _gc(self, now: float) -> None:
        if now < self._gc_at:
            return
        self._gc_at = now + 600
        for uid in [u for u, st in self._st.items() if now - st.last_seen > 3600 and st.muted_until < now]:
            del self._st[uid]

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        msg = event if isinstance(event, Message) else event.message
        if tg_user is None or tg_user.is_bot or msg is None or msg.chat.type != "private" or fraud.is_exempt(tg_user.id):
            return await handler(event, data)

        now = time.time()
        self._gc(now)
        st = self._st.setdefault(tg_user.id, _S())
        if now - st.last_seen > 3600:  # час тишины — страйки обнуляются
            st.strikes = 0
        st.last_seen = now

        if st.muted_until > now:
            await self._notify(event, st, now, f"🔇 Ты слишком часто нажимаешь. Подожди {int(st.muted_until - now) + 1} сек.")
            return None

        while st.hits and now - st.hits[0] > 60:
            st.hits.popleft()
        st.hits.append(now)
        recent = sum(1 for t in st.hits if now - t <= config.RATE_WINDOW)
        if recent <= config.RATE_BURST and len(st.hits) <= config.RATE_PER_MIN:
            return await handler(event, data)

        # превышение: событие отбрасываем
        st.drops += 1
        if st.drops >= config.RATE_DROPS_PER_STRIKE:
            st.drops = 0
            st.strikes += 1
            steps = config.RATE_MUTE_STEPS
            mute = steps[min(st.strikes, len(steps)) - 1]
            st.muted_until = now + mute
            st.hits.clear()
            await fraud.add_event(tg_user.id, "flood", f"страйк {st.strikes}, мут {mute} c")
            await moderation.evaluate(data["bot"], tg_user.id)
            await self._notify(event, st, now, f"🔇 Слишком много запросов. Пауза {mute} сек.", force=True)
        else:
            await self._notify(event, st, now, "⏳ Не так быстро!")
        return None

    @staticmethod
    async def _notify(event: Message | CallbackQuery, st: _S, now: float, text: str, force: bool = False) -> None:
        """Отвечаем редко (раз в 5 секунд), иначе сами станем спамить."""
        if not force and now - st.told < 5:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except TelegramAPIError:
                    pass
            return
        st.told = now
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=False)
            else:
                await event.answer(text)
        except TelegramAPIError:
            pass
