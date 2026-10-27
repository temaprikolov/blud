from __future__ import annotations

import html
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MSK = timedelta(hours=3)


def fmt_stars(cents: int) -> str:
    """Сотые доли звезды -> '0.49'."""
    return f"{(cents or 0) / 100:.2f}"


def fmt_short(cents: int) -> str:
    """Без лишних нулей: 1100 -> '11', 49 -> '0.49'."""
    return f"{(cents or 0) / 100:.2f}".rstrip("0").rstrip(".")


def bar(value: int, goal: int, width: int = 10) -> str:
    """▰▰▰▱▱▱▱▱▱▱ 30%"""
    ratio = 0.0 if goal <= 0 else max(0.0, min(1.0, value / goal))
    filled = int(ratio * width)
    return "▰" * filled + "▱" * (width - filled) + f" {int(ratio * 100)}%"


def parse_stars(text: str) -> int | None:
    """'15', '0.49', '0,5' -> сотые доли. None, если это не положительное число."""
    try:
        value = Decimal((text or "").strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value > 1_000_000:
        return None
    cents = int((value * 100).to_integral_value(ROUND_HALF_UP))
    return cents if cents > 0 else None


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return (dt + MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _bold(user, text: str, bold: bool | None = None) -> str:
    """Премиум-пользователи (Telegram Premium) выделяются жирным везде, где показывается ник."""
    if bold is None:
        bold = bool(getattr(user, "is_premium", False))
    return f"<b>{text}</b>" if bold else text


def nick(user, bold: bool | None = None) -> str:
    """Для админки: @username, иначе имя, иначе ID. Premium — жирным (bold=True — жирным всегда)."""
    if user.username:
        return _bold(user, "@" + esc(user.username), bold)
    return _bold(user, esc(user.full_name) or str(user.id), bold)


def mention(user) -> str:
    """Кликабельное упоминание: @username, а без него — ссылка по ID на имя. Premium — жирным."""
    if user.username:
        return _bold(user, "@" + esc(user.username))
    return _bold(user, f'<a href="tg://user?id={user.id}">{esc(user.full_name) or user.id}</a>')


def public_name(user) -> str:
    """Для топов: ник частично скрыт (уже экранирован под HTML). Premium — жирным."""
    if user.username:
        u = user.username
        return _bold(user, "@" + esc(u[:3] if len(u) > 3 else u[:1]) + "***")
    return _bold(user, f"ID …{str(user.id)[-4:]}")
