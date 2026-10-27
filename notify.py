"""Уведомления по заявкам на вывод: админам — о новой заявке, и обновление этих сообщений после закрытия."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import config
import fraud
import keyboards as kb
import services
from utils import fmt_dt, fmt_short, fmt_stars, mention

log = logging.getLogger(__name__)
LINE = "━━━━━━━━━━━━━━━━━━"


def request_text(w, user, closer: str | None = None, fscore: int = 0) -> str:
    text = (
        f"💫 <b>Заявка на вывод №{w.id}</b>\n{LINE}\n\n"
        f"👤 {mention(user)} · <code>{user.id}</code>\n"
        f"💎 Сумма: <b>{fmt_short(w.amount)} ⭐</b>\n"
        f"💰 Баланс в боте: {fmt_stars(user.balance)} ⭐\n"
        f"🕒 Создана: {fmt_dt(w.created_at)}\n"
        + (f"🛡 Фрод-очки: <b>{fscore}</b>\n" if fscore else "")
        + "\n"
    )
    if w.status == "pending":
        return text + "⏳ <b>Ожидает обработки</b>"
    who = f" · {closer}" if closer else ""
    if w.status == "done":
        return text + f"✅ <b>Закрыта</b>{who}\n{fmt_dt(w.closed_at)}\nСписано с баланса: {fmt_short(w.amount)} ⭐"
    return text + f"❌ <b>Отклонена</b>{who}\n{fmt_dt(w.closed_at)}\nЗвёзды с баланса не списывались"


async def notify_admins_new(bot: Bot, w, user) -> int:
    """Рассылает заявку всем админам. Возвращает, скольким дошло."""
    delivered = 0
    fscore = await fraud.score(user.id)
    for admin_id in sorted(config.ADMIN_IDS):
        try:
            m = await bot.send_message(admin_id, request_text(w, user, fscore=fscore), reply_markup=kb.wd_request_kb(w.id, user.id))
            await services.add_withdrawal_notice(w.id, admin_id, m.message_id)
            delivered += 1
        except TelegramAPIError as e:
            log.warning("не удалось уведомить админа %s о заявке %s: %s", admin_id, w.id, e)
    return delivered


async def refresh_notices(bot: Bot, w, user, closer: str | None) -> None:
    """После закрытия/отклонения обновляет сообщение о заявке у всех админов и убирает кнопки."""
    text = request_text(w, user, closer, await fraud.score(user.id))
    for n in await services.withdrawal_notices(w.id):
        try:
            await bot.edit_message_text(text, chat_id=n.admin_id, message_id=n.message_id)
        except TelegramAPIError:
            pass  # не изменилось / сообщение удалено — не страшно
