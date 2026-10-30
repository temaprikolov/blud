from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup as Kb

import config
import services
import settings
from utils import fmt_short

# Цвета кнопок в Telegram — всего три: success (зелёный), primary (синий), danger (красный).
G, P, R = "success", "primary", "danger"


def B(text: str, cb: str | None = None, url: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, url=url, style=style)


def back(to: str = "menu", text: str = "⬅️ Назад") -> Kb:
    return Kb(inline_keyboard=[[B(text, to)]])


# ───────────── пользователь ─────────────
def main_menu(is_admin: bool = False) -> Kb:
    rows = [
        [B("⭐ Заработать звёзды", "earn", style=G)],
        [B("👥 Рефералы", "refs", style=P), B("🪪 Профиль", "profile", style=P)],
        [B("💫 Вывод", "withdraw", style=R), B("📊 Статистика", "stats", style=R)],
        [B("📢 Наш канал", url=config.DEFAULT_CHANNEL_URL, style=G)],
    ]
    if is_admin:
        rows.append([B("🛠 Админ-панель", "a:menu", style=P)])
    return Kb(inline_keyboard=rows)


def earn_menu() -> Kb:
    return Kb(inline_keyboard=[[B("▶️ Выполнить задание", "task_next", style=G)], [B("⬅️ Назад", "menu")]])


def task_kb(task_id: int, url: str) -> Kb:
    return Kb(
        inline_keyboard=[
            [B("🔗 Перейти к заданию", url=url, style=P)],
            [B("✅ Проверить", f"t_check:{task_id}", style=G), B("⏭ Пропустить", f"t_skip:{task_id}", style=R)],
            [B("⬅️ Назад", "earn")],
        ]
    )


def withdraw_menu() -> Kb:
    amounts = settings.withdraw_amounts()
    rows, row = [], []
    for a in amounts:
        row.append(B(f"{a} ⭐", f"wd:{a}", style=G))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("📋 Мои заявки", "my_wd", style=P)])
    rows.append([B("⬅️ Назад", "menu")])
    return Kb(inline_keyboard=rows)


def stats_menu() -> Kb:
    return Kb(
        inline_keyboard=[
            [B("💰 Топ по балансу", "top:balance", style=G)],
            [B("👥 Топ по рефералам", "top:refs", style=P)],
            [B("🏆 Топ по заработку", "top:earned", style=R)],
            [B("⬅️ Назад", "menu")],
        ]
    )


def sub_wall(channels) -> Kb:
    rows = [[B(f"{'🔗' if getattr(c, 'is_link', False) else '📢'} {c.title}", url=c.url, style=P)] for c in channels]
    rows.append([B("✅ Я подписался", "check_sub", style=G)])
    return Kb(inline_keyboard=rows)


def sponsors_wall(tasks) -> Kb:
    rows = [[B(f"📢 {t.button}: {t.title}"[:60], url=t.link, style=P)] for t in tasks]
    rows.append([B("✅ Проверить подписку", "sp_check", style=G)])
    return Kb(inline_keyboard=rows)


# ───────────── админка ─────────────
def admin_menu(pending_withdrawals: int = 0) -> Kb:
    wd_text = f"💫 Заявки на вывод ({pending_withdrawals})" if pending_withdrawals else "💫 Заявки на вывод"
    return Kb(
        inline_keyboard=[
            [B(wd_text, "a:wd_list", style=R if pending_withdrawals else P)],
            [B("📈 Счётчики", "a:counters", style=P), B("🏅 Топы", "a:top:balance", style=P)],
            [B("👤 Найти пользователя", "a:find", style=G)],
            [B("📝 Задания", "a:tasks", style=G), B("📢 Каналы", "a:channels", style=G)],
            [B("🧾 Чеки", "a:checks", style=G), B("✉️ Рассылка", "a:broadcast", style=P)],
            [B("⚙️ Настройки", "a:settings", style=P), B("📤 Экспорт CSV", "a:export", style=P)],
            [B("⬅️ В меню бота", "menu")],
        ]
    )


def admin_back() -> Kb:
    return back("a:menu")


def admin_user_kb(user_id: int, banned: bool, hidden: bool = False) -> Kb:
    return Kb(
        inline_keyboard=[
            [B("➕ Начислить", f"a:bonus:{user_id}", style=G), B("➖ Списать", f"a:deduct:{user_id}", style=R)],
            [B("✉️ Написать ему", f"a:dm:{user_id}", style=P), B("🛡 Доверять", f"a:trust:{user_id}", style=G)],
            [B("👁 Вернуть в топ" if hidden else "🙈 Убрать из топа", f"a:hidetop:{user_id}", style=G if hidden else P)],
            [B("✅ Разбанить", f"a:ban:{user_id}", style=G) if banned else B("🚫 Забанить", f"a:ban:{user_id}", style=R)],
            [B("⬅️ Назад", "a:menu")],
        ]
    )


def cancel_kb() -> Kb:
    return Kb(inline_keyboard=[[B("✖️ Отмена", "a:menu", style=R)]])


def broadcast_categories(counts: dict[str, int]) -> Kb:
    rows = [
        [B(f"{label} · {counts.get(key, 0)}", f"a:bcat:{key}", style=G if key == "all" else P)]
        for key, label in services.BROADCAST_CATEGORIES.items()
    ]
    rows.append([B("⬅️ Назад", "a:menu")])
    return Kb(inline_keyboard=rows)


def confirm_broadcast_kb() -> Kb:
    return Kb(inline_keyboard=[[B("🚀 Отправить", "a:bc_go", style=G)], [B("✖️ Отмена", "a:menu", style=R)]])


def admin_tops(active: str) -> Kb:
    def tab(key: str, label: str) -> InlineKeyboardButton:
        return B(f"• {label} •" if key == active else label, f"a:top:{key}", style=G if key == active else P)

    return Kb(
        inline_keyboard=[
            [tab("balance", "💰 Баланс"), tab("earned", "🏆 Заработок"), tab("refs", "👥 Рефералы")],
            [B("⬅️ Назад", "a:menu")],
        ]
    )


SETTING_TITLES = {
    "task_reward": "💰 Награда за задание",
    "ref_reward": "🎁 Награда за друга",
    "ref_tasks": "🎯 Заданий для засчёта друга",
    "withdraw_amounts": "💫 Суммы вывода",
    "withdraw_term": "⏳ Срок вывода",
    "task_rate": "⏱ Лимит заданий в минуту",
}


def setting_display(key: str) -> str:
    if key == "task_reward":
        return f"{fmt_short(settings.task_reward())} ⭐"
    if key == "ref_reward":
        return f"{fmt_short(settings.ref_reward())} ⭐"
    if key == "ref_tasks":
        return str(settings.ref_tasks())
    if key == "task_rate":
        return str(settings.task_rate())
    if key == "withdraw_amounts":
        return ", ".join(str(a) for a in settings.withdraw_amounts())
    return settings.withdraw_term()


def settings_kb() -> Kb:
    rows = [[B(f"{title}: {setting_display(key)}", f"a:set:{key}", style=P)] for key, title in SETTING_TITLES.items()]
    rows.append([B("⬅️ Назад", "a:menu")])
    return Kb(inline_keyboard=rows)


def setting_edit_kb(key: str) -> Kb:
    return Kb(
        inline_keyboard=[
            [B("♻️ Вернуть по умолчанию", f"a:setreset:{key}", style=R)],
            [B("✖️ Отмена", "a:settings")],
        ]
    )


# ───────────── заявки на вывод ─────────────
def wd_request_kb(withdrawal_id: int, user_id: int) -> Kb:
    return Kb(
        inline_keyboard=[
            [
                B("✅ Закрыть заявку", f"a:wd_close:{withdrawal_id}", style=G),
                B("❌ Отклонить", f"a:wd_reject:{withdrawal_id}", style=R),
            ],
            [
                B("👤 Профиль", f"a:user:{user_id}", style=P),
                B("🚫 Забанить", f"a:wdban:{user_id}", style=R),
            ],
        ]
    )


def wd_list_kb(items, back_to: str = "a:menu", history: bool = False, reject_all: int = 0) -> Kb:
    """items: [(Withdrawal, User)]; reject_all — сколько заявок в ожидании (>0 → кнопка «Отклонить все»)."""
    rows = []
    for w, u in items:
        mark = {"pending": "⏳", "done": "✅", "rejected": "❌"}[w.status]
        who = f"@{u.username}" if u.username else (u.full_name or str(u.id))[:20]
        rows.append([B(f"{mark} №{w.id} · {who} · {fmt_short(w.amount)} ⭐", f"a:wd:{w.id}", style=P if not history else None)])
    if reject_all and not history:
        rows.append([B(f"🗑 Отклонить все ({reject_all})", "a:wd_all", style=R)])
    rows.append(
        [B("🕘 История", "a:wd_hist", style=P)] if not history else [B("⏳ Ожидающие", "a:wd_list", style=R)]
    )
    rows.append([B("⬅️ Назад", back_to)])
    return Kb(inline_keyboard=rows)


def wd_reject_all_kb() -> Kb:
    """Массовое отклонение: выбор — с уведомлением пользователей или без."""
    return Kb(
        inline_keyboard=[
            [B("🔕 Отклонить все — без уведомления", "a:wd_all:silent", style=R)],
            [B("🔔 Отклонить все — с уведомлением", "a:wd_all:notify", style=R)],
            [B("✖️ Отмена", "a:wd_list")],
        ]
    )


def wd_detail_kb(withdrawal_id: int, user_id: int, pending: bool) -> Kb:
    rows = []
    if pending:
        rows.append(
            [
                B("✅ Закрыть заявку", f"a:wd_close:{withdrawal_id}", style=G),
                B("❌ Отклонить", f"a:wd_reject:{withdrawal_id}", style=R),
            ]
        )
    rows.append(
        [
            B("👤 Профиль", f"a:user:{user_id}", style=P),
            B("🚫 Забанить", f"a:wdban:{user_id}", style=R),
        ]
    )
    rows.append([B("⬅️ К заявкам", "a:wd_list")])
    return Kb(inline_keyboard=rows)


# ───────────── чеки ─────────────
def checks_list_kb(checks) -> Kb:
    rows = [[B("➕ Создать чек", "a:chk_new", style=G)]]
    for c in checks:
        mark = "🟢" if services.check_problem(c) is None else "⚪️"
        limit = f"{c.activations}/{c.max_people}" if c.max_people is not None else f"{c.activations}"
        rows.append([B(f"{mark} №{c.id} · {fmt_short(c.per_person)} ⭐ · {limit}", f"a:chk:{c.id}", style=P)])
    rows.append([B("⬅️ Назад", "a:menu")])
    return Kb(inline_keyboard=rows)


def check_type_kb() -> Kb:
    return Kb(
        inline_keyboard=[
            [B("👥 По количеству человек", "a:chk_type:count", style=G)],
            [B("⏱ По времени", "a:chk_type:time", style=P)],
            [B("✖️ Отмена", "a:checks", style=R)],
        ]
    )


def check_equal_kb(per_person_cents: int) -> Kb:
    return Kb(
        inline_keyboard=[
            [B(f"Поровну: {fmt_short(per_person_cents)} ⭐ каждому", "a:chk_equal", style=G)],
            [B("✖️ Отмена", "a:checks", style=R)],
        ]
    )


def check_password_kb() -> Kb:
    return Kb(
        inline_keyboard=[
            [B("🔓 Без пароля", "a:chk_pw:no", style=G), B("🔐 Задать пароль", "a:chk_pw:yes", style=P)],
            [B("✖️ Отмена", "a:checks", style=R)],
        ]
    )


def check_channels_kb(has_channels: bool, can_add: bool) -> Kb:
    rows = []
    if can_add:
        rows.append([B("➕ Добавить канал для подписки", "a:chk_ch:add", style=P)])
    rows.append([B("✅ Создать чек" if has_channels else "✅ Без подписки — создать чек", "a:chk_ch:done", style=G)])
    rows.append([B("✖️ Отмена", "a:checks", style=R)])
    return Kb(inline_keyboard=rows)


def check_detail_kb(chk) -> Kb:
    return Kb(
        inline_keyboard=[
            [B("📨 Пост для пересылки", f"a:chk_post:{chk.id}", style=G), B("📣 В канал", f"a:chk_pub:{chk.id}", style=P)],
            [
                B("👥 Активации", f"a:chk_acts:{chk.id}", style=P),
                B("🛑 Отключить", f"a:chk_toggle:{chk.id}", style=R) if chk.is_active else B("✅ Включить", f"a:chk_toggle:{chk.id}", style=G),
            ],
            [B("⬅️ К чекам", "a:checks")],
        ]
    )


def check_publish_kb(check_id: int, channels) -> Kb:
    rows = [[B(f"📢 {c.title}", f"a:chk_pubto:{check_id}:{c.chat_id}", style=P)] for c in channels]
    rows.append([B("⬅️ Назад", f"a:chk:{check_id}")])
    return Kb(inline_keyboard=rows)


def check_post_kb(link: str) -> Kb:
    return Kb(inline_keyboard=[[B("🎁 Активировать чек", url=link, style=G)]])


def check_subscribe_kb(channels: list[dict], code: str) -> Kb:
    rows = [[B(f"📢 {c['title']}", url=c["url"], style=P)] for c in channels]
    rows.append([B("✅ Проверить подписку", f"chk_go:{code}", style=G)])
    return Kb(inline_keyboard=rows)
