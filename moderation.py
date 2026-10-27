"""Баны и реакция на подозрения: бан с автоматической чисткой данных, уведомления админам, оценка очков фрода."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup

import config
import fraud
import keyboards as kb
import services
from utils import esc, fmt_short, mention

log = logging.getLogger(__name__)


def _forget_runtime(uid: int) -> None:
    """Сбрасывает оперативную память по пользователю (кэш подписки, ожидающий чек)."""
    import middlewares  # локально: middlewares тянет много зависимостей

    middlewares.forget_sub_cache(uid)
    middlewares.PENDING_CHECKS.pop(uid, None)


def report_text(report: dict) -> str:
    parts = [f"заявок удалено: {report['withdrawals']}"]
    if report["balance"]:
        parts.append(f"баланс обнулён: {fmt_short(report['balance'])} ⭐")
    if report["checks"]:
        parts.append(f"активаций чеков отменено: {report['checks']}")
    if report["clawback"]:
        parts.append(f"возвращено рефереру {report['referrer_id']}: {fmt_short(report['clawback'])} ⭐")
    return ", ".join(parts)


async def _alert_admins(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None, skip: int | None = None) -> None:
    for admin_id in sorted(config.ADMIN_IDS):
        if admin_id == skip:
            continue
        try:
            await bot.send_message(admin_id, text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramAPIError:
            pass


async def ban_user(bot: Bot, uid: int, reason: str, by: int | None = None) -> dict:
    """Единая точка бана: помечает забаненным, чистит заявки/баланс/чеки/рефералку,
    удаляет у админов сообщения об удалённых заявках и сообщает, что именно было сделано."""
    report = await services.set_banned(uid, True) or {}
    report.setdefault("notices", [])
    for admin_id, message_id in report["notices"]:
        try:
            await bot.delete_message(admin_id, message_id)
        except TelegramAPIError:
            pass  # сообщение уже удалено / старше 48 часов — не страшно
    _forget_runtime(uid)

    user = await services.get_user(uid)
    who = mention(user) if user else str(uid)
    head = "🤖 <b>Автобан (анти-фрод)</b>" if by is None else "🚫 <b>Бан</b>"
    await _alert_admins(
        bot,
        f"{head}\n{who} · <code>{uid}</code>\nПричина: {esc(reason)}\n🧹 {esc(report_text(report))}",
        InlineKeyboardMarkup(inline_keyboard=[[kb.B("✅ Разбанить", f"a:ban:{uid}", style=kb.G)]]),
        skip=by,
    )
    return report


async def unban_user(uid: int) -> None:
    await services.set_banned(uid, False)


async def evaluate(bot: Bot, uid: int) -> None:
    """Смотрит на очки пользователя: достаточно много — банит, средне — замораживает и зовёт админа."""
    if fraud.is_exempt(uid):
        return
    user = await services.get_user(uid)
    if not user or user.is_banned:
        return
    total = await fraud.score(uid)
    if total >= config.FRAUD_BAN:
        evs = await fraud.events(uid, 5)
        why = "; ".join(fraud.KINDS.get(e.kind, (0, e.kind))[1] for e in evs if e.points > 0) or "набрал очки подозрительности"
        await ban_user(bot, uid, f"{why} ({total} очк.)")
    elif total >= config.FRAUD_HOLD and await fraud.add_event(uid, "alerted", once=True):
        evs = await fraud.events(uid, 6)
        lines = "\n".join(f"• {esc(fraud.KINDS.get(e.kind, (0, e.kind))[1])} ({e.points:+d}) {esc(e.note)}" for e in evs if e.kind != "alerted")
        await _alert_admins(
            bot,
            f"⚠️ <b>Подозрительный пользователь</b>\n{mention(user)} · <code>{uid}</code>\n"
            f"Очки: <b>{total}</b> (заморозка ≥ {config.FRAUD_HOLD}, автобан ≥ {config.FRAUD_BAN})\n\n{lines}\n\n"
            "Реферальные награды и вывод заморожены, пока ты не решишь.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [kb.B("🚫 Забанить", f"a:ban:{uid}", style=kb.R), kb.B("✅ Доверять", f"a:trust:{uid}", style=kb.G)],
                    [kb.B("👤 Профиль", f"a:user:{uid}", style=kb.P)],
                ]
            ),
        )
