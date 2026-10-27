"""Короткий журнал действий: одна строка = одно действие. Файл actions.log рядом с ботом.

    10-27 15:01:22  7727571734 @name  CB       withdraw:15
    10-27 15:01:23  7727571734 @name  WD_REQ   15⭐ заявка №42

Время — московское. Файл можно целиком отправить Claude и спросить: «что делал @name», «кто вывел 15 звёзд»,
«кто мог положить бота» (смотри строки FLOOD / SLOW / ERR / NET и кто стоит перед ними).
Админу в боте: /log — последние 1000 строк файлом; /log 300; /log @name; /log 123456789; /log wd (по типу).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import config

MSK = timezone(timedelta(hours=3))
PATH = config.BASE_DIR / "actions.log"
SLOW_SEC = 3.0  # обработка дольше этого — пишем SLOW (кто «вешает» бота)
CAP_PER_MIN = 40  # больше строк MSG/CB от одного человека в минуту не пишем (считаем) — иначе флудер забьёт журнал

_names: dict[int, str] = {}
_cap: dict[int, list] = {}  # uid -> [начало минуты, записано, пропущено]

_logger = logging.getLogger("actions")
_logger.setLevel(logging.INFO)
_logger.propagate = False
try:
    _h = RotatingFileHandler(PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_h)
except OSError:
    _logger.addHandler(logging.NullHandler())


def _who(uid: int) -> str:
    if not uid:
        return "SYSTEM".ljust(19)
    return f"{uid} {_names.get(uid, '')}".ljust(19)


def event(uid: int, kind: str, detail: str = "") -> None:
    """Запись в журнал. Никогда не бросает исключений."""
    try:
        ts = datetime.now(MSK).strftime("%m-%d %H:%M:%S")
        _logger.info("%s  %s %s %s", ts, _who(uid), kind.ljust(8), str(detail).replace("\n", " ")[:200])
    except Exception:  # noqa: BLE001
        pass


def _throttled(uid: int) -> bool:
    now = time.time()
    c = _cap.get(uid)
    if not c or now - c[0] >= 60:
        if c and c[2]:
            event(uid, "SKIPPED", f"ещё {c[2]} действий за минуту (журнал урезан)")
        _cap[uid] = [now, 1, 0]
        if len(_cap) > 5000:  # чистка
            for k in [k for k, v in _cap.items() if now - v[0] > 300]:
                _cap.pop(k, None)
        return False
    if c[1] >= CAP_PER_MIN:
        c[2] += 1
        return True
    c[1] += 1
    return False


def _describe(ev: Message | CallbackQuery) -> tuple[str, str]:
    if isinstance(ev, CallbackQuery):
        return "CB", (ev.data or "")[:64]
    t = ev.text or ev.caption or ""
    if t.startswith("/"):
        return "MSG", t[:60]  # команды и /start-параметры — видно, что именно нажал
    if t:
        return "MSG", f"текст[{len(t)}]"  # содержимое обычных сообщений (пароли чеков и т.п.) не пишем
    return "MSG", f"<{ev.content_type}>"


class AuditMiddleware(BaseMiddleware):
    """Ставится первым: видит все события, даже те, что потом отрежет анти-спам."""

    async def __call__(self, handler: Callable[[Any, dict[str, Any]], Awaitable[Any]], event_: Message | CallbackQuery,
                       data: dict[str, Any]) -> Any:
        u = data.get("event_from_user")
        if u is None or u.is_bot:
            return await handler(event_, data)
        _names[u.id] = f"@{u.username}" if u.username else (u.full_name or "")[:20].replace(" ", "_")
        kind, detail = _describe(event_)
        if not _throttled(u.id):
            event(u.id, kind, detail)
        t0 = time.monotonic()
        try:
            return await handler(event_, data)
        finally:
            dt = time.monotonic() - t0
            if dt >= SLOW_SEC:
                event(u.id, "SLOW", f"{dt:.1f}c на {kind} {detail}")


def uid_of(update: Any) -> int:
    for part in ("message", "callback_query"):
        obj = getattr(update, part, None)
        if obj is not None and getattr(obj, "from_user", None):
            return obj.from_user.id
    return 0


# ───────── /log для админов ─────────
router = Router(name="audit")


def _read_lines() -> list[str]:
    out: list[str] = []
    for p in (PATH.with_name(PATH.name + ".1"), PATH):
        try:
            out += p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return out


@router.message(Command("log"))
async def cmd_log(m: Message) -> None:
    if not m.from_user or m.from_user.id not in config.ADMIN_IDS:
        return
    args = (m.text or "").split()[1:]
    lines = _read_lines()
    limit = 1000
    for a in args:
        if a.lstrip("@").isdigit() and len(a) < 6:
            limit = int(a)
        elif a.startswith("@") or a.isdigit():
            key = a.lower()
            lines = [l for l in lines if key in l.lower()]
        else:
            key = a.upper()
            lines = [l for l in lines if key in l[34:46].upper()]
    lines = lines[-limit:]
    if not lines:
        await m.answer("Ничего не найдено.")
        return
    data = "\n".join(lines).encode("utf-8")
    await m.answer_document(BufferedInputFile(data, filename="actions.txt"), caption=f"Строк: {len(lines)}")
