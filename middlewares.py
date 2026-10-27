from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

import config
import fraud
import keyboards as kb
import moderation
import services
import sponsors

log = logging.getLogger(__name__)

_ok_cache: dict[int, float] = {}  # user_id -> до какого момента подписка считается проверенной
CACHE_TTL = 120
NO_CHECK_WAIT = 10  # секунд ожидания для каналов без проверки подписки
_banned_told: dict[int, float] = {}  # забаненному отвечаем не чаще раза в минуту
_waiting: set[int] = set()  # кто сейчас ждёт — чтобы повторные нажатия не запускали ожидание заново

# Ссылка на чек, открытая до подписки на обязательные каналы: user_id -> код чека.
# Нужна, чтобы после «Я подписался» чек активировался сам, а не терялся.
PENDING_CHECKS: dict[int, str] = {}


async def is_member(bot: Bot, chat_id: int, user_id: int) -> Optional[bool]:
    """True/False, либо None, если проверить нельзя (бот не админ канала и т.п.)."""
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as e:
        log.warning("get_chat_member(%s, %s) failed: %s", chat_id, user_id, e)
        return None
    if m.status in ("left", "kicked"):
        return False
    if m.status == "restricted":
        return bool(getattr(m, "is_member", True))
    return True


async def unsubscribed_channels(bot: Bot, user_id: int, use_cache: bool = True):
    if use_cache and _ok_cache.get(user_id, 0) > time.time():
        return []
    missing = []
    confirmed: set[int] | None = None
    miss_ids: list[int] = []
    ok_ids: list[int] = []
    channels = await services.list_channels()
    checked = [ch for ch in channels if ch.check_enabled]
    results = await asyncio.gather(*(is_member(bot, ch.chat_id, user_id) for ch in checked))  # параллельно, а не по очереди
    res_by_id = {ch.id: r for ch, r in zip(checked, results)}
    for ch in channels:
        if ch.check_enabled:
            r = res_by_id[ch.id]
            if r is False:
                missing.append(ch)
                miss_ids.append(ch.id)
            elif r:
                ok_ids.append(ch.id)
        else:  # без проверки: считаем подписанным тех, кто уже прошёл ожидание
            if confirmed is None:
                confirmed = await services.confirmed_channel_ids(user_id)
            if ch.id not in confirmed:
                missing.append(ch)
    try:  # статистика «сколько подписалось с бота»: не должна ломать вход
        if miss_ids:
            await services.mark_missing(user_id, miss_ids)
        if ok_ids:
            await services.convert_passes(user_id, ok_ids)
    except Exception as e:  # noqa: BLE001
        log.warning("Статистика каналов: %s", e)
    if not missing:
        _ok_cache[user_id] = time.time() + CACHE_TTL
    else:
        _ok_cache.pop(user_id, None)
    return missing


def forget_sub_cache(user_id: int) -> None:
    _ok_cache.pop(user_id, None)


class AccessMiddleware(BaseMiddleware):
    """Регистрирует пользователя, режет забаненных, требует подписку на каналы."""

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        msg = event if isinstance(event, Message) else event.message
        if tg_user is None or tg_user.is_bot or msg is None or msg.chat.type != "private":
            return await handler(event, data)

        referrer_id = None
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            parts = event.text.split(maxsplit=1)
            payload = parts[1] if len(parts) == 2 else ""
            if payload.startswith("ref_") and payload[4:].isdigit():
                referrer_id = int(payload[4:])
            if payload.startswith("chk_") and 4 < len(payload) <= 40:
                PENDING_CHECKS[tg_user.id] = payload[4:]
            else:
                PENDING_CHECKS.pop(tg_user.id, None)

        user, created = await services.register_user(tg_user, referrer_id)
        data["user"] = user
        is_admin = tg_user.id in config.ADMIN_IDS
        data["is_admin"] = is_admin

        if is_admin:
            return await handler(event, data)

        if created:  # анти-фрод: оцениваем новый аккаунт и пачки рефералов
            try:
                await fraud.on_register(tg_user, user.referrer_id)
                await moderation.evaluate(data["bot"], tg_user.id)
                if user.referrer_id:
                    await moderation.evaluate(data["bot"], user.referrer_id)
            except Exception as e:  # noqa: BLE001 — анти-фрод не должен ломать вход
                log.warning("anti-fraud on_register: %s", e)
            user = await services.get_user(tg_user.id) or user
            data["user"] = user

        if user.is_banned:
            now = time.time()
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Доступ ограничен", show_alert=True)
            elif now - _banned_told.get(tg_user.id, 0) > 60:
                _banned_told[tg_user.id] = now
                await event.answer("🚫 Доступ к боту ограничен.")
            return None

        bot: Bot = data["bot"]
        force = isinstance(event, CallbackQuery) and event.data == "check_sub"
        missing = await unsubscribed_channels(bot, tg_user.id, use_cache=not force)
        if missing and force and isinstance(event, CallbackQuery) and not any(c.check_enabled for c in missing):
            # остались только каналы без проверки: ждём NO_CHECK_WAIT секунд и считаем подписку подтверждённой
            if tg_user.id in _waiting:
                await event.answer("⏳ Подожди, подписка проверяется…")
                return None
            _waiting.add(tg_user.id)
            try:
                await event.answer()
                try:
                    await msg.edit_text(f"⏳ <b>Проверяем подписку…</b>\n\nПодожди {NO_CHECK_WAIT} секунд.")
                except TelegramAPIError:
                    pass
                await asyncio.sleep(NO_CHECK_WAIT)
                await services.confirm_channels(tg_user.id, [c.id for c in missing])
                forget_sub_cache(tg_user.id)
            finally:
                _waiting.discard(tg_user.id)
            return await handler(event, data)

        if missing:
            text = "📢 Чтобы пользоваться ботом, подпишись на каналы ниже, затем нажми «Я подписался»."
            if isinstance(event, CallbackQuery):
                hard = ", ".join(c.title for c in missing if c.check_enabled)
                alert = f"Ты ещё не подписался: {hard}"[:190] if hard else "Ты ещё не подписался на все каналы"
                await event.answer(alert if force else None, show_alert=force)
                if not force:
                    await msg.answer(text, reply_markup=kb.sub_wall(missing))
            else:
                await event.answer(text, reply_markup=kb.sub_wall(missing))
            return None

        # спонсорские подписки (Traffy / PiarFlow; Axionna — только в заданиях): на /start и по кнопке «Проверить»
        if sponsors.wall_enabled():
            is_start = isinstance(event, Message) and bool(event.text) and event.text.startswith("/start")
            is_cb = isinstance(event, CallbackQuery) and event.data == "sp_check"
            if is_start or is_cb:
                forced = isinstance(event, CallbackQuery) and event.data == "sp_check"
                tasks = await sponsors.gate(tg_user, force=forced)
                if tasks:
                    text = "📢 <b>Подпишись на спонсоров</b> ниже, затем нажми «Проверить подписку» — и доступ откроется."
                    if forced:
                        await event.answer("Ты ещё не подписался на всех спонсоров", show_alert=True)
                        try:  # обновляем список на месте (часть спонсоров могла уже выполниться)
                            await msg.edit_text(text, reply_markup=kb.sponsors_wall(tasks), disable_web_page_preview=True)
                        except TelegramAPIError:
                            pass
                    else:
                        if isinstance(event, CallbackQuery):
                            await event.answer()
                        await msg.answer(text, reply_markup=kb.sponsors_wall(tasks), disable_web_page_preview=True)
                    return None
                if isinstance(event, CallbackQuery) and event.data == "sp_check":
                    await event.answer("✅ Готово")
                    from handlers_user import menu_text  # noqa: E402
                    fresh = await services.get_user(tg_user.id)
                    await msg.answer(menu_text(fresh), reply_markup=kb.main_menu(False))
                    return None

        return await handler(event, data)
