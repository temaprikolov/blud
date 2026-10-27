"""Вся работа с БД. Суммы везде в сотых долях звезды."""
from __future__ import annotations

import json
import math
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

import config
import fraud
import settings
from db import Channel, ChannelMiss, ChannelPass, Check, CheckActivation, SessionLocal, Task, Transaction, User, UserTask, Withdrawal, WithdrawalNotice, utcnow


# ───────────────────────── пользователи ─────────────────────────
async def register_user(tg_user, referrer_id: Optional[int] = None) -> tuple[User, bool]:
    """Создаёт пользователя при первом заходе. Реферер фиксируется только тогда."""
    async with SessionLocal() as s:
        user = await s.get(User, tg_user.id)
        if user:
            changed = False
            if user.username != tg_user.username:
                user.username, changed = tg_user.username, True
            if user.full_name != (tg_user.full_name or ""):
                user.full_name, changed = tg_user.full_name or "", True
            prem = bool(getattr(tg_user, "is_premium", None))
            if user.is_premium != prem:
                user.is_premium, changed = prem, True
            if user.is_blocked:
                user.is_blocked, changed = False, True
            if utcnow() - user.last_active > timedelta(seconds=60):
                user.last_active, changed = utcnow(), True
            if changed:
                await s.commit()
            return user, False

        ref = None
        if referrer_id and referrer_id != tg_user.id and await s.get(User, referrer_id):
            ref = referrer_id
        user = User(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name or "",
            referrer_id=ref,
            is_premium=bool(getattr(tg_user, "is_premium", None)),
        )
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:  # параллельный /start
            await s.rollback()
            return await s.get(User, tg_user.id), False
        return user, True


async def get_user(user_id: int) -> Optional[User]:
    async with SessionLocal() as s:
        return await s.get(User, user_id)


async def find_user(query: str) -> Optional[User]:
    query = query.strip()
    async with SessionLocal() as s:
        if query.lstrip("-").isdigit():
            return await s.get(User, int(query))
        name = query.lstrip("@").lower()
        return (
            await s.execute(select(User).where(func.lower(User.username) == name))
        ).scalars().first()


async def set_blocked(user_id: int, blocked: bool) -> None:
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_blocked=blocked))
        await s.commit()


async def set_hide_top(user_id: int, hidden: bool) -> None:
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user_id).values(hide_top=hidden))
        await s.commit()


async def set_banned(user_id: int, banned: bool) -> Optional[dict]:
    """Бан/разбан. При бане ВСЕ данные пользователя чистятся автоматически (purge_user_data) —
    возвращается отчёт о том, что удалено. При разбане сбрасываются очки подозрительности."""
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_banned=banned))
        await s.commit()
    if banned:
        return await purge_user_data(user_id)
    await fraud.clear(user_id)
    return None


async def purge_user_data(user_id: int) -> dict:
    """Чистка забаненного, одной транзакцией:
      • заявки на вывод в ожидании и отклонённые + служебные сообщения о них у админов;
      • баланс обнуляется (записью «ban_wipe» в историю — можно вернуть вручную, если бан ошибочный);
      • активации чеков (место в чеке освобождается);
      • отметки подписок на каналы (чтобы не накручивать статистику каналов);
      • награда, выданная его рефереру за него, возвращается (clawback), а сам он выпадает из ТОПа рефералов.
    Закрытые (выплаченные) заявки остаются — это бухгалтерия: звёзды реально ушли."""
    report = {"withdrawals": 0, "balance": 0, "checks": 0, "clawback": 0, "referrer_id": None, "notices": []}
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if not user:
            return report

        wds = list(
            (await s.execute(select(Withdrawal).where(Withdrawal.user_id == user_id, Withdrawal.status.in_(("pending", "rejected"))))).scalars()
        )
        ids = [w.id for w in wds]
        if ids:
            notices = list((await s.execute(select(WithdrawalNotice).where(WithdrawalNotice.withdrawal_id.in_(ids)))).scalars())
            report["notices"] = [(n.admin_id, n.message_id) for n in notices]
            await s.execute(delete(WithdrawalNotice).where(WithdrawalNotice.withdrawal_id.in_(ids)))
            await s.execute(delete(Withdrawal).where(Withdrawal.id.in_(ids)))
        report["withdrawals"] = len(ids)

        if user.balance > 0:
            report["balance"] = user.balance
            s.add(Transaction(user_id=user_id, amount=-user.balance, kind="ban_wipe", note="обнуление баланса при бане"))
            user.balance = 0

        acts = list((await s.execute(select(CheckActivation).where(CheckActivation.user_id == user_id))).scalars())
        for a in acts:
            await s.execute(update(Check).where(Check.id == a.check_id, Check.activations > 0).values(activations=Check.activations - 1))
        if acts:
            await s.execute(delete(CheckActivation).where(CheckActivation.user_id == user_id))
        report["checks"] = len(acts)

        await s.execute(delete(ChannelPass).where(ChannelPass.user_id == user_id))
        await s.execute(delete(ChannelMiss).where(ChannelMiss.user_id == user_id))

        user.ref_top_counted = False
        if user.referrer_id and user.ref_counted:
            ref_tx = (
                await s.execute(
                    select(Transaction).where(
                        Transaction.user_id == user.referrer_id, Transaction.kind == "referral", Transaction.note == f"друг {user_id}"
                    )
                )
            ).scalars().first()
            referrer = await s.get(User, user.referrer_id)
            if ref_tx and referrer:
                back = min(ref_tx.amount, referrer.balance)
                referrer.earned = max(0, referrer.earned - ref_tx.amount)
                referrer.ref_earned = max(0, referrer.ref_earned - ref_tx.amount)
                if back > 0:
                    referrer.balance -= back
                    s.add(Transaction(user_id=referrer.id, amount=-back, kind="clawback", note=f"возврат награды: друг {user_id} забанен"))
                report["clawback"], report["referrer_id"] = back, referrer.id
        await s.commit()
    return report


async def referral_counts(user_id: int) -> tuple[int, int]:
    """(всего приглашено, засчитано)."""
    async with SessionLocal() as s:
        row = (
            await s.execute(
                select(
                    func.count(User.id),
                    func.coalesce(func.sum(case((User.ref_counted.is_(True), 1), else_=0)), 0),
                ).where(User.referrer_id == user_id)
            )
        ).one()
        return int(row[0]), int(row[1])


async def withdrawn_total(user_id: int) -> int:
    async with SessionLocal() as s:
        v = await s.scalar(
            select(func.coalesce(func.sum(-Transaction.amount), 0)).where(
                Transaction.user_id == user_id, Transaction.kind == "deduct"
            )
        )
        return int(v or 0)


async def last_transactions(user_id: int, limit: int = 5) -> list[Transaction]:
    async with SessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(Transaction)
                    .where(Transaction.user_id == user_id)
                    .order_by(Transaction.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )


# ───────────────────────── задания ─────────────────────────
async def next_task(user_id: int) -> Optional[Task]:
    async with SessionLocal() as s:
        seen = select(UserTask.task_id).where(UserTask.user_id == user_id)
        return (
            await s.execute(
                select(Task).where(Task.is_active.is_(True), Task.source == "own", Task.id.not_in(seen)).order_by(Task.id).limit(1)
            )
        ).scalars().first()


async def get_net_task(source: str, title: str, url: str) -> Task:
    """Локальная запись задания от рекламной сети (одна на ссылку) — чтобы работали награда, лимиты и учёт."""
    async with SessionLocal() as s:
        t = (await s.execute(select(Task).where(Task.source == source, Task.url == url[:500]))).scalars().first()
        if t is None:
            t = Task(title=title[:200], url=url[:500], chat_id=None, source=source, is_active=True)
            s.add(t)
            await s.commit()
        return t


async def has_user_task(user_id: int, task_id: int) -> bool:
    async with SessionLocal() as s:
        return (await s.execute(select(UserTask.id).where(UserTask.user_id == user_id, UserTask.task_id == task_id))).first() is not None


async def get_task(task_id: int) -> Optional[Task]:
    async with SessionLocal() as s:
        return await s.get(Task, task_id)


async def _cooldown_wait(s, user_id: int) -> int:
    """Секунд до следующего разрешённого задания (0 — можно). Окно скользящее, 60 секунд."""
    limit = settings.task_rate()
    now = utcnow()
    recent = list(
        (
            await s.execute(
                select(UserTask.created_at)
                .where(
                    UserTask.user_id == user_id,
                    UserTask.status == "done",
                    UserTask.created_at >= now - timedelta(seconds=60),
                )
                .order_by(UserTask.created_at)
            )
        ).scalars()
    )
    if len(recent) < limit:
        return 0
    free_at = recent[len(recent) - limit] + timedelta(seconds=60)
    return max(1, math.ceil((free_at - now).total_seconds()))


async def task_cooldown(user_id: int) -> int:
    async with SessionLocal() as s:
        return await _cooldown_wait(s, user_id)


async def complete_task(user_id: int, task_id: int) -> Optional[dict]:
    """Начисляет награду за задание. None — нельзя (уже сделано / задание удалено или выключено).

    Если превышен лимит заданий в минуту — {'wait': секунд}, ничего не начисляется.
    Иначе {'reward': int, 'referrer_id': int | None, 'ref_reward': int} — referrer_id заполнен,
    если именно это задание стало нужным по счёту и реферер получил свою награду.
    """
    reward = settings.task_reward()
    ref_reward = settings.ref_reward()
    ref_needed = settings.ref_tasks()
    async with SessionLocal() as s:
        task = await s.get(Task, task_id)
        if not task or not task.is_active:
            return None
        if task.reward:  # у задания своя цена, заданная админом; иначе — стандартная
            reward = int(task.reward)

        wait = await _cooldown_wait(s, user_id)
        if wait:
            return {"wait": wait}

        s.add(UserTask(user_id=user_id, task_id=task_id, status="done"))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return None

        await s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                balance=User.balance + reward,
                earned=User.earned + reward,
                tasks_done=User.tasks_done + 1,
            )
        )
        s.add(Transaction(user_id=user_id, amount=reward, kind="task", note=task.title[:200]))

        user = await s.get(User, user_id)
        await s.refresh(user)
        rewarded_ref: Optional[int] = None

        hold = False
        if user.referrer_id and not user.ref_counted and user.tasks_done >= ref_needed:
            # награду за реферала не выдаём, пока сам он или его реферер под подозрением / забанен:
            # она не пропадает — выдастся на следующем задании, когда подозрение снимут
            referrer_row = await s.get(User, user.referrer_id)
            hold = (
                referrer_row is None
                or referrer_row.is_banned
                or user.is_banned
                or await fraud.score(user_id, s) >= config.FRAUD_HOLD
                or await fraud.score(user.referrer_id, s) >= config.FRAUD_HOLD
            )
        if user.referrer_id and not user.ref_counted and user.tasks_done >= ref_needed and not hold:
            claim = await s.execute(
                update(User).where(User.id == user_id, User.ref_counted.is_(False)).values(ref_counted=True, ref_top_counted=True)
            )
            if claim.rowcount == 1:
                await s.execute(
                    update(User)
                    .where(User.id == user.referrer_id)
                    .values(
                        balance=User.balance + ref_reward,
                        earned=User.earned + ref_reward,
                        ref_earned=User.ref_earned + ref_reward,
                    )
                )
                s.add(
                    Transaction(
                        user_id=user.referrer_id,
                        amount=ref_reward,
                        kind="referral",
                        note=f"друг {user_id}",
                    )
                )
                rewarded_ref = user.referrer_id

        await s.commit()
        return {"reward": reward, "referrer_id": rewarded_ref, "ref_reward": ref_reward, "tasks_done": user.tasks_done}


async def skip_task(user_id: int, task_id: int) -> bool:
    async with SessionLocal() as s:
        s.add(UserTask(user_id=user_id, task_id=task_id, status="skipped"))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return False
        await s.execute(update(User).where(User.id == user_id).values(tasks_skipped=User.tasks_skipped + 1))
        await s.commit()
        return True


async def add_task(title: str, url: str, chat_id: Optional[int], reward: Optional[int] = None) -> Task:
    async with SessionLocal() as s:
        task = Task(title=title, url=url, chat_id=chat_id, reward=reward)
        s.add(task)
        await s.commit()
        return task


async def list_tasks() -> list[tuple[Task, int]]:
    """Задания с числом выполнений."""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(
                    Task,
                    func.coalesce(
                        select(func.count(UserTask.id))
                        .where(UserTask.task_id == Task.id, UserTask.status == "done")
                        .correlate(Task)
                        .scalar_subquery(),
                        0,
                    ),
                ).where(Task.source == "own").order_by(Task.id.desc())
            )
        ).all()
        return [(r[0], int(r[1])) for r in rows]


async def toggle_task(task_id: int) -> None:
    async with SessionLocal() as s:
        task = await s.get(Task, task_id)
        if task:
            task.is_active = not task.is_active
            await s.commit()


async def delete_task(task_id: int) -> None:
    async with SessionLocal() as s:
        await s.execute(delete(Task).where(Task.id == task_id))
        await s.commit()


# ───────────────────────── каналы ─────────────────────────
async def list_channels() -> list[Channel]:
    async with SessionLocal() as s:
        return list((await s.execute(select(Channel).order_by(Channel.id))).scalars())


async def add_channel(chat_id: Optional[int], title: str, url: str, check_enabled: bool = True, is_link: bool = False) -> bool:
    """chat_id=None допустим только без проверки: тогда подставляется служебный отрицательный ID
    (вне диапазона настоящих чатов Telegram) — он нужен лишь из-за уникальности поля и в API не передаётся."""
    if chat_id is None:
        chat_id = -9_000_000_000_000_000 - secrets.randbelow(10**12)
    async with SessionLocal() as s:
        if is_link or not check_enabled:  # одинаковую ссылку дважды не добавляем
            dup = (await s.execute(select(Channel.id).where(Channel.url == url))).first()
            if dup:
                return False
        s.add(Channel(chat_id=chat_id, title=title[:200], url=url, check_enabled=check_enabled, is_link=is_link))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return False
        return True


async def delete_channel(channel_id: int) -> None:
    async with SessionLocal() as s:
        await s.execute(delete(Channel).where(Channel.id == channel_id))
        await s.execute(delete(ChannelPass).where(ChannelPass.channel_id == channel_id))
        await s.execute(delete(ChannelMiss).where(ChannelMiss.channel_id == channel_id))
        await s.commit()


async def confirmed_channel_ids(user_id: int) -> set[int]:
    async with SessionLocal() as s:
        return set((await s.execute(select(ChannelPass.channel_id).where(ChannelPass.user_id == user_id))).scalars())


async def confirm_channels(user_id: int, channel_ids: list[int]) -> None:
    async with SessionLocal() as s:
        have = set((await s.execute(select(ChannelPass.channel_id).where(ChannelPass.user_id == user_id))).scalars())
        for cid in channel_ids:
            if cid not in have:
                s.add(ChannelPass(user_id=user_id, channel_id=cid))
        try:
            await s.commit()
        except IntegrityError:  # параллельное подтверждение — уже записано
            await s.rollback()


async def mark_missing(user_id: int, channel_ids: list[int]) -> None:
    """Запоминаем: на этих каналах с проверкой человек не был подписан, когда пришёл в бота."""
    async with SessionLocal() as s:
        have = set((await s.execute(select(ChannelMiss.channel_id).where(ChannelMiss.user_id == user_id))).scalars())
        new = [c for c in channel_ids if c not in have]
        if not new:
            return
        for cid in new:
            s.add(ChannelMiss(user_id=user_id, channel_id=cid))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()


async def convert_passes(user_id: int, channel_ids: list[int]) -> None:
    """Человек теперь подписан на каналы с проверкой. Если раньше он не был подписан — это подписка «с нашего бота»."""
    async with SessionLocal() as s:
        missed = set(
            (await s.execute(select(ChannelMiss.channel_id).where(ChannelMiss.user_id == user_id, ChannelMiss.channel_id.in_(channel_ids)))).scalars()
        )
        if not missed:
            return
        have = set((await s.execute(select(ChannelPass.channel_id).where(ChannelPass.user_id == user_id))).scalars())
        for cid in missed - have:
            s.add(ChannelPass(user_id=user_id, channel_id=cid))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()


async def channel_counts() -> dict[int, int]:
    """channel_id -> сколько человек подписались/выполнили условие через бота."""
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelPass.channel_id, func.count(ChannelPass.id)).group_by(ChannelPass.channel_id))).all()
        return {r[0]: int(r[1]) for r in rows}


async def seed_default_channel() -> None:
    """Канал бота подключается как обязательный один раз — на самом первом запуске."""
    async with SessionLocal() as s:
        if await s.scalar(select(func.count(Channel.id))) == 0 and not await s.scalar(
            select(func.count(User.id))
        ):
            s.add(
                Channel(
                    chat_id=config.DEFAULT_CHANNEL_ID,
                    title=config.DEFAULT_CHANNEL_TITLE,
                    url=config.DEFAULT_CHANNEL_URL,
                )
            )
            await s.commit()


# ───────────────────────── баланс (админка) ─────────────────────────
async def deduct_balance(user_id: int, cents: int, admin_id: int) -> Optional[int]:
    """Списывает не больше, чем есть на балансе. Возвращает списанную сумму (None — нет юзера)."""
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if not user:
            return None
        actual = min(cents, user.balance)
        if actual <= 0:
            return 0
        res = await s.execute(
            update(User).where(User.id == user_id, User.balance >= actual).values(balance=User.balance - actual)
        )
        if res.rowcount != 1:
            await s.rollback()
            return 0
        s.add(Transaction(user_id=user_id, amount=-actual, kind="deduct", note="списание админом", admin_id=admin_id))
        await s.commit()
        return actual


# ───────────────────────── статистика ─────────────────────────
async def public_stats() -> dict:
    async with SessionLocal() as s:
        users = await s.scalar(select(func.count(User.id)).where(User.id.not_in(config.HIDDEN_IDS)))
        paid = await s.scalar(
            select(func.coalesce(func.sum(-Transaction.amount), 0)).where(
                Transaction.kind == "deduct", Transaction.user_id.not_in(config.HIDDEN_IDS)
            )
        )
        return {"users": int(users or 0), "paid": int(paid or 0)}


async def admin_counters() -> dict:
    now = utcnow()
    msk_midnight = (now + timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)

    async def count(*where) -> int:
        q = select(func.count(User.id))
        if where:
            q = q.where(and_(*where))
        return int(await s.scalar(q) or 0)

    async with SessionLocal() as s:
        return {
            "total": await count(),
            "today": await count(User.created_at >= msk_midnight),
            "week": await count(User.created_at >= now - timedelta(days=7)),
            "active24": await count(User.last_active >= now - timedelta(hours=24)),
            "with_tasks": await count(User.tasks_done > 0),
            "no_tasks": await count(User.tasks_done == 0),
            "from_ref": await count(User.referrer_id.is_not(None)),
            "ref_counted": await count(User.ref_counted.is_(True)),
            "with_balance": await count(User.balance > 0),
            "blocked": await count(User.is_blocked.is_(True)),
            "banned": await count(User.is_banned.is_(True)),
            "total_balance": int(await s.scalar(select(func.coalesce(func.sum(User.balance), 0))) or 0),
            "total_earned": int(await s.scalar(select(func.coalesce(func.sum(User.earned), 0))) or 0),
            "checks_paid": int(await s.scalar(select(func.coalesce(func.sum(CheckActivation.amount), 0))) or 0),
            "paid": int(
                await s.scalar(
                    select(func.coalesce(func.sum(-Transaction.amount), 0)).where(Transaction.kind == "deduct")
                )
                or 0
            ),
            "tasks_total": int(await s.scalar(select(func.coalesce(func.sum(User.tasks_done), 0))) or 0),
        }


async def top_by(field: str, limit: int = 10) -> list[tuple[User, int]]:
    """field: balance | earned | refs."""
    async with SessionLocal() as s:
        if field == "refs":
            cnt = func.count(User.id).label("cnt")
            inv = (
                select(User.referrer_id.label("rid"), cnt)
                .where(
                    User.ref_top_counted.is_(True),
                    User.referrer_id.is_not(None),
                    User.referrer_id.not_in(config.HIDDEN_IDS),
                    User.referrer_id.not_in(config.HIDDEN_REF_TOP_IDS),
                    User.id.not_in(config.HIDDEN_IDS),
                )
                .group_by(User.referrer_id)
                .order_by(cnt.desc())
                .limit(limit)
                .subquery()
            )
            rows = (
                await s.execute(select(User, inv.c.cnt).join(inv, inv.c.rid == User.id).where(User.is_banned.is_(False), User.hide_top.is_(False)).order_by(inv.c.cnt.desc()))
            ).all()
            return [(r[0], int(r[1])) for r in rows]

        col = User.balance if field == "balance" else User.earned
        rows = (await s.execute(select(User, col).where(col > 0, User.id.not_in(config.HIDDEN_IDS), User.is_banned.is_(False), User.hide_top.is_(False)).order_by(col.desc()).limit(limit))).all()
        return [(r[0], int(r[1])) for r in rows]


BROADCAST_CATEGORIES = {
    "all": "👥 Все пользователи",
    "with_tasks": "✅ Выполнявшие задания",
    "no_tasks": "🆕 Без заданий",
    "ref": "🔗 Пришедшие по рефералке",
    "balance": "💰 С балансом > 0",
    "active": "🔥 Активные за 24 ч",
}


def _category_filter(category: str):
    if category == "with_tasks":
        return User.tasks_done > 0
    if category == "no_tasks":
        return User.tasks_done == 0
    if category == "ref":
        return User.referrer_id.is_not(None)
    if category == "balance":
        return User.balance > 0
    if category == "active":
        return User.last_active >= utcnow() - timedelta(hours=24)
    return None


async def broadcast_ids(category: str = "all") -> list[int]:
    q = select(User.id).where(User.is_blocked.is_(False), User.is_banned.is_(False))
    flt = _category_filter(category)
    if flt is not None:
        q = q.where(flt)
    async with SessionLocal() as s:
        return list((await s.execute(q)).scalars())


async def broadcast_counts() -> dict[str, int]:
    return {key: len(await broadcast_ids(key)) for key in BROADCAST_CATEGORIES}


async def add_bonus(user_id: int, cents: int, admin_id: int) -> bool:
    """Ручное начисление от админа."""
    async with SessionLocal() as s:
        res = await s.execute(
            update(User)
            .where(User.id == user_id)
            .values(balance=User.balance + cents, earned=User.earned + cents)
        )
        if res.rowcount != 1:
            await s.rollback()
            return False
        s.add(Transaction(user_id=user_id, amount=cents, kind="bonus", note="начисление админом", admin_id=admin_id))
        await s.commit()
        return True


async def export_users() -> list[dict]:
    async with SessionLocal() as s:
        users = list((await s.execute(select(User).order_by(User.created_at))).scalars())
        invited: dict[int, int] = {}
        for u in users:
            if u.referrer_id:
                invited[u.referrer_id] = invited.get(u.referrer_id, 0) + 1
        paid = dict(
            (await s.execute(
                select(Transaction.user_id, func.sum(-Transaction.amount))
                .where(Transaction.kind == "deduct")
                .group_by(Transaction.user_id)
            )).all()
        )
    return [
        {
            "id": u.id,
            "username": u.username or "",
            "name": u.full_name,
            "balance": u.balance / 100,
            "earned": u.earned / 100,
            "ref_earned": u.ref_earned / 100,
            "withdrawn": (paid.get(u.id) or 0) / 100,
            "tasks_done": u.tasks_done,
            "tasks_skipped": u.tasks_skipped,
            "invited": invited.get(u.id, 0),
            "referrer_id": u.referrer_id or "",
            "ref_counted": int(u.ref_counted),
            "banned": int(u.is_banned),
            "blocked_bot": int(u.is_blocked),
            "registered": u.created_at.isoformat(sep=" ", timespec="seconds"),
            "last_active": u.last_active.isoformat(sep=" ", timespec="seconds"),
        }
        for u in users
    ]



# ───────────── заявки на вывод ─────────────
async def reserved_for_withdrawals(user_id: int) -> int:
    """Сколько звёзд сейчас «заморожено» в ожидающих заявках."""
    async with SessionLocal() as s:
        v = await s.scalar(
            select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                Withdrawal.user_id == user_id, Withdrawal.status == "pending"
            )
        )
        return int(v or 0)


async def create_withdrawal(user_id: int, stars: int) -> Optional[Withdrawal]:
    """Создаёт заявку, если свободного (не занятого другими заявками) баланса хватает."""
    cents = stars * 100
    async with SessionLocal() as s:
        user = await s.get(User, user_id)
        if not user:
            return None
        reserved = await s.scalar(
            select(func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                Withdrawal.user_id == user_id, Withdrawal.status == "pending"
            )
        )
        if user.balance - int(reserved or 0) < cents:
            return None
        w = Withdrawal(user_id=user_id, amount=cents)
        s.add(w)
        await s.commit()
        return w


async def add_withdrawal_notice(withdrawal_id: int, admin_id: int, message_id: int) -> None:
    async with SessionLocal() as s:
        s.add(WithdrawalNotice(withdrawal_id=withdrawal_id, admin_id=admin_id, message_id=message_id))
        await s.commit()


async def withdrawal_notices(withdrawal_id: int) -> list[WithdrawalNotice]:
    async with SessionLocal() as s:
        return list(
            (await s.execute(select(WithdrawalNotice).where(WithdrawalNotice.withdrawal_id == withdrawal_id))).scalars()
        )


async def get_withdrawal(withdrawal_id: int) -> Optional[tuple[Withdrawal, User]]:
    async with SessionLocal() as s:
        w = await s.get(Withdrawal, withdrawal_id)
        if not w:
            return None
        return w, await s.get(User, w.user_id)


async def list_withdrawals(status: str, limit: int = 20) -> list[tuple[Withdrawal, User]]:
    """status: pending | closed (done + rejected)."""
    async with SessionLocal() as s:
        q = select(Withdrawal, User).join(User, User.id == Withdrawal.user_id)
        if status == "pending":
            q = q.where(Withdrawal.status == "pending").order_by(Withdrawal.id)
        else:
            q = q.where(Withdrawal.status != "pending").order_by(Withdrawal.id.desc())
        return [(r[0], r[1]) for r in (await s.execute(q.limit(limit))).all()]


async def user_withdrawals(user_id: int, limit: int = 10) -> list[Withdrawal]:
    async with SessionLocal() as s:
        return list(
            (
                await s.execute(
                    select(Withdrawal).where(Withdrawal.user_id == user_id).order_by(Withdrawal.id.desc()).limit(limit)
                )
            ).scalars()
        )


async def pending_withdrawal_count() -> int:
    async with SessionLocal() as s:
        return int(await s.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "pending")) or 0)


async def user_withdrawal_stats(user_id: int) -> tuple[int, int]:
    """(ожидают, выполнено)."""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Withdrawal.status, func.count(Withdrawal.id)).where(Withdrawal.user_id == user_id).group_by(Withdrawal.status)
            )
        ).all()
    counts = {r[0]: int(r[1]) for r in rows}
    return counts.get("pending", 0), counts.get("done", 0)


async def close_withdrawal(withdrawal_id: int, admin_id: int) -> tuple[str, Optional[Withdrawal]]:
    """Закрывает заявку и списывает звёзды с баланса.

    -> ('ok' | 'missing' | 'already' | 'insufficient', заявка)
    """
    async with SessionLocal() as s:
        w = await s.get(Withdrawal, withdrawal_id)
        if not w:
            return "missing", None
        if w.status != "pending":
            return "already", w
        user = await s.get(User, w.user_id)
        if not user or user.balance < w.amount:
            return "insufficient", w
        # статус и списание — одной транзакцией; повторное нажатие второго админа ничего не спишет
        claim = await s.execute(
            update(Withdrawal)
            .where(Withdrawal.id == withdrawal_id, Withdrawal.status == "pending")
            .values(status="done", closed_at=utcnow(), closed_by=admin_id)
        )
        if claim.rowcount != 1:
            await s.rollback()
            return "already", await s.get(Withdrawal, withdrawal_id)
        debit = await s.execute(
            update(User).where(User.id == w.user_id, User.balance >= w.amount).values(balance=User.balance - w.amount)
        )
        if debit.rowcount != 1:
            await s.rollback()
            return "insufficient", w
        s.add(
            Transaction(
                user_id=w.user_id,
                amount=-w.amount,
                kind="deduct",
                note=f"вывод, заявка №{w.id}",
                admin_id=admin_id,
            )
        )
        await s.commit()
        await s.refresh(w)
        return "ok", w


async def reject_withdrawal(withdrawal_id: int, admin_id: int) -> tuple[str, Optional[Withdrawal]]:
    """Отклоняет заявку без списания. -> ('ok' | 'missing' | 'already', заявка)"""
    async with SessionLocal() as s:
        w = await s.get(Withdrawal, withdrawal_id)
        if not w:
            return "missing", None
        res = await s.execute(
            update(Withdrawal)
            .where(Withdrawal.id == withdrawal_id, Withdrawal.status == "pending")
            .values(status="rejected", closed_at=utcnow(), closed_by=admin_id)
        )
        if res.rowcount != 1:
            await s.rollback()
            return "already", w
        await s.commit()
        await s.refresh(w)
        return "ok", w



# ───────────── чеки ─────────────
def check_channels(chk: Check) -> list[dict]:
    """[{'chat_id', 'title', 'url'}] — подписки, нужные для активации."""
    try:
        return json.loads(chk.channels or "[]")
    except ValueError:
        return []


async def create_check(
    per_person: int,
    max_people: Optional[int],
    expires_at: Optional[datetime],
    password: Optional[str],
    channels: list[dict],
    admin_id: int,
) -> Check:
    async with SessionLocal() as s:
        for _ in range(10):
            chk = Check(
                code=secrets.token_urlsafe(6),
                per_person=per_person,
                max_people=max_people,
                expires_at=expires_at,
                password=password,
                channels=json.dumps(channels, ensure_ascii=False),
                created_by=admin_id,
            )
            s.add(chk)
            try:
                await s.commit()
                return chk
            except IntegrityError:  # коллизия кода — крайне маловероятна
                await s.rollback()
        raise RuntimeError("не удалось сгенерировать уникальный код чека")


async def get_check(check_id: int) -> Optional[Check]:
    async with SessionLocal() as s:
        return await s.get(Check, check_id)


async def get_check_by_code(code: str) -> Optional[Check]:
    async with SessionLocal() as s:
        return (await s.execute(select(Check).where(Check.code == code))).scalars().first()


async def list_checks(limit: int = 30) -> list[Check]:
    async with SessionLocal() as s:
        return list((await s.execute(select(Check).order_by(Check.id.desc()).limit(limit))).scalars())


async def toggle_check(check_id: int) -> None:
    async with SessionLocal() as s:
        chk = await s.get(Check, check_id)
        if chk:
            chk.is_active = not chk.is_active
            await s.commit()


async def check_activation_list(check_id: int, limit: int = 30) -> list[tuple[CheckActivation, User]]:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(CheckActivation, User)
                .join(User, User.id == CheckActivation.user_id)
                .where(CheckActivation.check_id == check_id)
                .order_by(CheckActivation.id.desc())
                .limit(limit)
            )
        ).all()
        return [(r[0], r[1]) for r in rows]


async def has_activated(check_id: int, user_id: int) -> bool:
    async with SessionLocal() as s:
        return bool(
            await s.scalar(
                select(func.count(CheckActivation.id)).where(
                    CheckActivation.check_id == check_id, CheckActivation.user_id == user_id
                )
            )
        )


def check_problem(chk: Check) -> Optional[str]:
    """'inactive' | 'expired' | 'exhausted' | None."""
    if not chk.is_active:
        return "inactive"
    if chk.expires_at and chk.expires_at <= utcnow():
        return "expired"
    if chk.max_people is not None and chk.activations >= chk.max_people:
        return "exhausted"
    return None


async def activate_check(user_id: int, code: str) -> tuple[str, Optional[Check], int]:
    """Зачисляет звёзды за чек одним действием.

    -> ('ok' | 'missing' | 'inactive' | 'expired' | 'exhausted' | 'already', чек, начислено_в_сотых)
    Пароль и подписки проверяет вызывающий код — тут только то, что должно быть атомарным.
    """
    async with SessionLocal() as s:
        chk = (await s.execute(select(Check).where(Check.code == code))).scalars().first()
        if not chk:
            return "missing", None, 0
        problem = check_problem(chk)
        if problem:
            return problem, chk, 0

        s.add(CheckActivation(check_id=chk.id, user_id=user_id, amount=chk.per_person))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return "already", chk, 0

        # занимаем «место» в чеке; если параллельно ушло последнее — откатываемся
        slot = await s.execute(
            update(Check)
            .where(
                Check.id == chk.id,
                Check.is_active.is_(True),
                or_(Check.max_people.is_(None), Check.activations < Check.max_people),
            )
            .values(activations=Check.activations + 1)
        )
        if slot.rowcount != 1:
            await s.rollback()
            return "exhausted", chk, 0

        await s.execute(
            update(User)
            .where(User.id == user_id)
            .values(balance=User.balance + chk.per_person, earned=User.earned + chk.per_person)
        )
        s.add(Transaction(user_id=user_id, amount=chk.per_person, kind="check", note=f"чек №{chk.id}"))
        await s.commit()
        await s.refresh(chk)
        return "ok", chk, chk.per_person


# ───────────────────────── журнал действий (audit.py) ─────────────────────────
# Оборачиваем ключевые операции: логика не меняется, лишь пишется строка в actions.log.
import audit  # noqa: E402


def _stars(cents: int) -> str:
    return f"{cents / 100:g}⭐"


def _audited(name: str):
    orig = globals()[name]

    def deco(fmt):
        async def wrapper(*a, **kw):
            res = await orig(*a, **kw)
            try:
                fmt(res, *a, **kw)
            except Exception:  # noqa: BLE001 — журнал не должен ломать операции
                pass
            return res

        wrapper.__name__ = name
        wrapper.__doc__ = orig.__doc__
        globals()[name] = wrapper
        return wrapper

    return deco


@_audited("complete_task")
def _(res, user_id, task_id, *a, **kw):
    if not res:
        audit.event(user_id, "TASK_NO", f"задание {task_id} недоступно")
    elif "wait" in res:
        audit.event(user_id, "TASK_WAIT", f"задание {task_id}, кулдаун {res['wait']}c")
    else:
        audit.event(user_id, "TASK_OK", f"задание {task_id} +{_stars(res['reward'])} (всего {res['tasks_done']})")
        if res.get("referrer_id"):
            audit.event(res["referrer_id"], "REF_PAY", f"+{_stars(res['ref_reward'])} за друга {user_id}")


@_audited("create_withdrawal")
def _(res, user_id, stars, *a, **kw):
    if res:
        audit.event(user_id, "WD_REQ", f"{stars}⭐ заявка №{res.id}")
    else:
        audit.event(user_id, "WD_FAIL", f"{stars}⭐ не создана (не хватает баланса)")


@_audited("close_withdrawal")
def _(res, withdrawal_id, admin_id, *a, **kw):
    status, w = res
    uid = w.user_id if w else 0
    audit.event(uid, "WD_DONE" if status == "ok" else "WD_ERR",
                f"заявка №{withdrawal_id} {_stars(w.amount) if w else ''} закрыл админ {admin_id} [{status}]")


@_audited("reject_withdrawal")
def _(res, withdrawal_id, admin_id, *a, **kw):
    status, w = res
    audit.event(w.user_id if w else 0, "WD_REJ" if status == "ok" else "WD_ERR",
                f"заявка №{withdrawal_id} {_stars(w.amount) if w else ''} отклонил админ {admin_id} [{status}]")


@_audited("set_banned")
def _(res, user_id, banned, *a, **kw):
    audit.event(user_id, "BAN" if banned else "UNBAN", "")


@_audited("add_bonus")
def _(res, user_id, cents, admin_id, *a, **kw):
    audit.event(user_id, "BONUS", f"+{_stars(cents)} от админа {admin_id}")


@_audited("deduct_balance")
def _(res, user_id, cents, admin_id, *a, **kw):
    audit.event(user_id, "DEDUCT", f"-{_stars(res or 0)} админом {admin_id}")


@_audited("activate_check")
def _(res, user_id, code, *a, **kw):
    status, chk, amount = res
    audit.event(user_id, "CHECK", f"чек {code[:12]} [{status}]" + (f" +{_stars(amount)}" if amount else ""))


@_audited("register_user")
def _(res, tg_user, referrer_id=None, *a, **kw):
    user, created = res
    if created:
        audit.event(tg_user.id, "NEW", f"новый пользователь" + (f", пригласил {user.referrer_id}" if user.referrer_id else ""))
