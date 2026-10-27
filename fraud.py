"""Анти-фрод: система очков подозрительности.

Каждый подозрительный сигнал — запись FraudEvent с очками. Сумма очков пользователя:
  >= config.FRAUD_HOLD — реферальная награда и вывод заморожены, админам приходит уведомление;
  >= config.FRAUD_BAN  — автоматический бан (moderation.ban_user: заявки и данные удаляются).
Положительные очки — признак бота, отрицательные — признак живого человека (Telegram Premium, «доверенный»).
Модуль не знает про Telegram-бота: только БД и расчёты.
"""
from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, func, select

import config
from db import FraudEvent, SessionLocal, User, UserTask, utcnow

# kind -> (очки, человекочитаемое название)
KINDS: dict[str, tuple[int, str]] = {
    "flood": (25, "флуд / спам запросами"),
    "no_lang": (10, "нет языка интерфейса"),
    "premium": (-15, "Telegram Premium"),
    "fast_click": (10, "проверка задания быстрее человека"),
    "robot_rhythm": (40, "задания ровно по таймеру (бот)"),
    "marathon": (50, "активен почти 24 часа подряд"),
    "ref_burst": (60, "массовые рефералы за короткое время"),
    "ref_burst_child": (25, "пришёл в пачке рефералов"),
    "trusted": (-1000, "отмечен админом как доверенный"),
    "alerted": (0, "админам отправлено уведомление"),
}


async def add_event(
    user_id: int,
    kind: str,
    note: str = "",
    *,
    points: Optional[int] = None,
    once: bool = False,
    within: Optional[timedelta] = None,
) -> bool:
    """Добавляет событие. once — только если такого ещё не было; within — если не было за этот период.
    True — событие записано."""
    pts = KINDS[kind][0] if points is None else points
    async with SessionLocal() as s:
        if once or within:
            q = select(func.count(FraudEvent.id)).where(FraudEvent.user_id == user_id, FraudEvent.kind == kind)
            if within:
                q = q.where(FraudEvent.created_at >= utcnow() - within)
            if await s.scalar(q):
                return False
        s.add(FraudEvent(user_id=user_id, kind=kind, points=pts, note=note[:200]))
        await s.commit()
    try:
        import audit
        audit.event(user_id, "FLOOD" if kind == "flood" else "FRAUD", f"{kind} {pts:+d} {note}"[:150])
    except Exception:  # noqa: BLE001
        pass
    return True


async def score(user_id: int, session=None) -> int:
    """Очки подозрительности. session — чтобы считать внутри уже открытой транзакции."""
    q = select(func.coalesce(func.sum(FraudEvent.points), 0)).where(FraudEvent.user_id == user_id)
    if session is not None:
        total = await session.scalar(q)
    else:
        async with SessionLocal() as s:
            total = await s.scalar(q)
    return max(0, int(total or 0))


async def has_event(user_id: int, kind: str) -> bool:
    async with SessionLocal() as s:
        return bool(await s.scalar(select(func.count(FraudEvent.id)).where(FraudEvent.user_id == user_id, FraudEvent.kind == kind)))


async def events(user_id: int, limit: int = 8) -> list[FraudEvent]:
    async with SessionLocal() as s:
        return list(
            (await s.execute(select(FraudEvent).where(FraudEvent.user_id == user_id).order_by(FraudEvent.id.desc()).limit(limit))).scalars()
        )


async def clear(user_id: int) -> None:
    """Сбрасывает всю историю подозрений (при разбане или «доверять»)."""
    async with SessionLocal() as s:
        await s.execute(delete(FraudEvent).where(FraudEvent.user_id == user_id))
        await s.commit()


async def trust(user_id: int) -> None:
    await clear(user_id)
    await add_event(user_id, "trusted")


async def top_suspicious(limit: int = 15) -> list[tuple[int, int]]:
    """[(user_id, очки)] — у кого очки > 0, по убыванию."""
    total = func.sum(FraudEvent.points).label("total")
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(FraudEvent.user_id, total).group_by(FraudEvent.user_id).having(total > 0).order_by(total.desc()).limit(limit)
            )
        ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


def is_exempt(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS or user_id in config.HIDDEN_IDS


# ───────────── сигналы ─────────────
async def on_register(tg_user, referrer_id: Optional[int]) -> None:
    """Вызывается один раз, когда пользователь создан. Смотрит на сам аккаунт и на пачки рефералов."""
    if is_exempt(tg_user.id):
        return
    if not getattr(tg_user, "language_code", None):
        await add_event(tg_user.id, "no_lang", once=True)
    if getattr(tg_user, "is_premium", None):
        await add_event(tg_user.id, "premium", once=True)

    if not referrer_id:
        return
    since = utcnow() - timedelta(minutes=10)
    async with SessionLocal() as s:
        ids = list((await s.execute(select(User.id).where(User.referrer_id == referrer_id, User.created_at >= since))).scalars())
    if len(ids) >= config.FRAUD_REF_BURST and not is_exempt(referrer_id):
        await add_event(referrer_id, "ref_burst", f"{len(ids)} рефералов за 10 минут", within=timedelta(hours=6))
        for uid in ids[:100]:
            await add_event(uid, "ref_burst_child", f"реферал {referrer_id}", once=True)


async def on_fast_click(user_id: int, elapsed: float) -> None:
    if elapsed < config.FRAUD_FAST_CLICK and not is_exempt(user_id):
        await add_event(user_id, "fast_click", f"{elapsed:.2f} c после показа")


async def check_task_pattern(user_id: int, tasks_done: int, per_minute: int) -> None:
    """Поведенческий анализ после засчитанного задания: ровный «машинный» ритм и активность сутками напролёт."""
    if is_exempt(user_id):
        return
    now = utcnow()
    async with SessionLocal() as s:
        recent = list(
            (
                await s.execute(
                    select(UserTask.created_at)
                    .where(UserTask.user_id == user_id, UserTask.status == "done")
                    .order_by(UserTask.id.desc())
                    .limit(per_minute + 8)
                )
            ).scalars()
        )
        day = []
        if tasks_done % 10 == 0:  # тяжёлую проверку делаем не на каждом задании
            day = list(
                (
                    await s.execute(
                        select(UserTask.created_at).where(
                            UserTask.user_id == user_id, UserTask.status == "done", UserTask.created_at >= now - timedelta(hours=24)
                        )
                    )
                ).scalars()
            )

    ts = sorted(recent)
    if len(ts) >= 9:
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        # а) промежутки между заданиями почти одинаковые — человек так не умеет
        if statistics.pstdev(gaps[-8:]) < 0.4 and statistics.mean(gaps[-8:]) < 120:
            await add_event(user_id, "robot_rhythm", "одинаковые интервалы", within=timedelta(hours=12))
        # б) каждое задание ровно в момент, когда кончился лимит «N в минуту»
        lim = max(1, per_minute)
        lagged = [(ts[i] - ts[i - lim]).total_seconds() for i in range(lim, len(ts))]
        if len(lagged) >= 6 and all(60.0 <= g <= 62.0 for g in lagged[-6:]):
            await add_event(user_id, "robot_rhythm", "задания ровно по таймеру лимита", within=timedelta(hours=12))
    if day:
        hours = {(t.year, t.month, t.day, t.hour) for t in day}
        if len(hours) >= 22:
            await add_event(user_id, "marathon", f"{len(hours)} из 24 часов", within=timedelta(hours=24))
