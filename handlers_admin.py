from __future__ import annotations

import asyncio
import csv
import io
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandStart, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Kb,
    Message,
)

import config
import fraud
import keyboards as kb
import moderation
import notify
import services
import settings
import sponsors
from handlers_user import show
from middlewares import NO_CHECK_WAIT
from utils import esc, fmt_dt, fmt_short, fmt_stars, mention, nick, parse_stars

LINE = "━━━━━━━━━━━━━━━━━━"

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and event.from_user.id in config.ADMIN_IDS)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class S(StatesGroup):
    find = State()
    deduct = State()
    bonus = State()
    dm = State()
    setting = State()
    task_title = State()
    task_url = State()
    task_chat = State()
    task_price = State()
    ch_chat = State()
    ch_link = State()
    ch_title = State()
    ch_url = State()
    lnk_url = State()
    lnk_title = State()
    broadcast = State()


short = fmt_short


async def admin_menu_kb():
    return kb.admin_menu(await services.pending_withdrawal_count())


async def admin_home_text() -> str:
    c = await services.admin_counters()
    return (
        f"🛠 <b>Админ-панель</b>\n{LINE}\n\n"
        f"👥 Пользователей: <b>{c['total']}</b>  (+{c['today']} сегодня)\n"
        f"🔥 Активных за 24 ч: <b>{c['active24']}</b>\n"
        f"✅ Заданий выполнено: <b>{c['tasks_total']}</b>\n"
        f"💰 На балансах: <b>{fmt_stars(c['total_balance'])} ⭐</b>\n"
        f"💸 Выплачено: <b>{fmt_stars(c['paid'])} ⭐</b>\n"
        f"💫 Заявок на вывод в ожидании: <b>{await services.pending_withdrawal_count()}</b>"
    )


# ───────────── вход ─────────────
@router.message(CommandStart(), StateFilter("*"))
async def admin_start_resets_state(message: Message, state: FSMContext) -> None:
    """/start сбрасывает незаконченный диалог и отдаёт обработку обычному меню."""
    await state.clear()
    raise SkipHandler()


@router.message(Command("traffy"))
async def cmd_sponsors(message: Message) -> None:
    """Диагностика спонсорских сетей: что вернёт каждая для самого админа."""
    if not sponsors.enabled():
        await message.answer("Спонсорские сети выключены: ключи в .env пустые.")
        return
    u = message.from_user
    lines = []
    for name, on, fn in (("Traffy", sponsors.traffy_enabled(), sponsors.traffy_fetch),
                         ("PiarFlow", sponsors.piarflow_enabled(), sponsors.pf_fetch),
                         ("Axionna", sponsors.axionna_enabled(), sponsors.ax_fetch)):
        if not on:
            lines.append(f"<b>{name}</b>: выключен")
            continue
        try:
            tasks = await fn(u)
            body = "\n".join(f"• {esc(t.title)} — {esc(t.link)}" for t in tasks) or "заданий нет (пускает)"
            lines.append(f"<b>{name}</b>: ✅ работает\n{body}")
        except Exception as e:
            lines.append(f"<b>{name}</b>: ❌ {esc(str(e))[:400]}")
    await message.answer("\n\n".join(lines), disable_web_page_preview=True)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await admin_home_text(), reply_markup=await admin_menu_kb())


@router.callback_query(F.data == "a:menu")
async def cb_admin_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show(cb, await admin_home_text(), await admin_menu_kb())


# ───────────── счётчики ─────────────
@router.callback_query(F.data == "a:counters")
async def cb_counters(cb: CallbackQuery) -> None:
    c = await services.admin_counters()
    await cb.answer()
    await show(
        cb,
        "📈 <b>Пользователи</b>\n\n"
        f"👥 Всего: <b>{c['total']}</b>\n\n"
        "<b>По времени</b>\n"
        f"├ Новых сегодня: {c['today']}\n"
        f"├ Новых за 7 дней: {c['week']}\n"
        f"└ Активных за 24 ч: {c['active24']}\n\n"
        "<b>По активности</b>\n"
        f"├ Выполнили ≥1 задание: {c['with_tasks']}\n"
        f"├ Не выполнили ни одного: {c['no_tasks']}\n"
        f"└ С ненулевым балансом: {c['with_balance']}\n\n"
        "<b>По источнику</b>\n"
        f"├ Пришли по реф. ссылке: {c['from_ref']}\n"
        f"├ Из них засчитано (5 заданий): {c['ref_counted']}\n"
        f"└ Пришли сами: {c['total'] - c['from_ref']}\n\n"
        "<b>Статус</b>\n"
        f"├ Заблокировали бота: {c['blocked']}\n"
        f"└ Забанены: {c['banned']}\n\n"
        "<b>Звёзды</b>\n"
        f"├ Заданий выполнено: {c['tasks_total']}\n"
        f"├ Всего начислено: {fmt_stars(c['total_earned'])} ⭐\n"
        f"├ Сейчас на балансах: {fmt_stars(c['total_balance'])} ⭐\n"
        f"├ Выдано по чекам: {fmt_stars(c['checks_paid'])} ⭐\n"
        f"└ Выплачено (списано): {fmt_stars(c['paid'])} ⭐",
        kb.admin_back(),
    )


# ───────────── поиск пользователя ─────────────
@router.callback_query(F.data == "a:find")
async def cb_find(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.find)
    await cb.answer()
    await show(cb, "👤 Отправь ID или @username пользователя:", kb.cancel_kb())


async def user_card(user_id: int) -> tuple[str, Kb] | None:
    u = await services.get_user(user_id)
    if not u:
        return None
    total, counted = await services.referral_counts(u.id)
    withdrawn = await services.withdrawn_total(u.id)
    txs = await services.last_transactions(u.id)
    referrer = "—"
    if u.referrer_id:
        r = await services.get_user(u.referrer_id)
        referrer = f"{nick(r)} (<code>{r.id}</code>)" if r else f"<code>{u.referrer_id}</code>"
    wd_pending, wd_done = await services.user_withdrawal_stats(u.id)
    kinds = {"task": "задание", "referral": "реферал", "deduct": "списание", "bonus": "бонус", "check": "чек", "ban_wipe": "обнуление при бане", "clawback": "возврат награды"}
    history = (
        "\n".join(
            f"{'+' if t.amount > 0 else '−'}{short(abs(t.amount))} ⭐ · {kinds.get(t.kind, t.kind)} · {fmt_dt(t.created_at)}"
            for t in txs
        )
        or "—"
    )
    fscore = await fraud.score(u.id)
    fev = [e for e in await fraud.events(u.id, 5) if e.kind != "alerted"]
    fraud_block = f"🛡 Фрод-очки: <b>{fscore}</b> (заморозка ≥ {config.FRAUD_HOLD}, автобан ≥ {config.FRAUD_BAN})"
    if fev:
        fraud_block += "\n" + "\n".join(f"  • {esc(fraud.KINDS.get(e.kind, (0, e.kind))[1])} ({e.points:+d})" for e in fev)
    text = (
        f"👤 {nick(u, bold=True)}{'  💎 <i>Premium</i>' if u.is_premium else ''}\n"
        f"ID: <code>{u.id}</code>\n"
        f"Имя: {esc(u.full_name) or '—'}\n"
        f"Регистрация: {fmt_dt(u.created_at)}\n"
        f"Последняя активность: {fmt_dt(u.last_active)}\n\n"
        f"💰 Баланс: <b>{fmt_stars(u.balance)} ⭐</b>\n"
        f"🏆 Всего заработано: {fmt_stars(u.earned)} ⭐\n"
        f"├ из них за рефералов: {fmt_stars(u.ref_earned)} ⭐\n"
        f"└ за задания: {fmt_stars(u.earned - u.ref_earned)} ⭐\n"
        f"💸 Списано (выплачено): {fmt_stars(withdrawn)} ⭐\n"
        f"💫 Заявки на вывод: ожидают {wd_pending}, выполнено {wd_done}\n\n"
        f"✅ Заданий выполнено: {u.tasks_done}\n"
        f"⏭ Пропущено: {u.tasks_skipped}\n\n"
        f"👥 Приглашено друзей: {total}\n"
        f"✔️ Засчитано: {counted}\n"
        f"Пригласил его: {referrer}\n"
        f"Его реф. награда выдана: {'да' if u.ref_counted else 'нет'}\n\n"
        f"Заблокировал бота: {'да' if u.is_blocked else 'нет'}\n"
        f"Бан: {'да 🚫' if u.is_banned else 'нет'}\n"
        f"В топах: {'скрыт 🙈' if (u.hide_top or u.is_banned) else 'виден'}\n"
        f"{fraud_block}\n\n"
        f"<b>Последние операции</b>\n{history}"
    )
    return text, kb.admin_user_kb(u.id, u.is_banned, u.hide_top)


@router.message(S.find)
async def msg_find(message: Message, state: FSMContext) -> None:
    found = await services.find_user(message.text or "")
    if not found:
        await message.answer("Пользователь не найден. Попробуй ещё раз или нажми «Отмена».", reply_markup=kb.cancel_kb())
        return
    await state.clear()
    text, markup = await user_card(found.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a:ban:"))
async def cb_ban(cb: CallbackQuery, bot: Bot) -> None:
    uid = int(cb.data.split(":")[2])
    u = await services.get_user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    if u.is_banned:
        await moderation.unban_user(uid)
        await cb.answer("✅ Разбанен, очки подозрительности сброшены")
    else:
        if uid in config.ADMIN_IDS:
            await cb.answer("Нельзя банить администратора", show_alert=True)
            return
        report = await moderation.ban_user(bot, uid, "бан администратором", by=cb.from_user.id)
        await cb.answer(f"🚫 Забанен. {moderation.report_text(report)}"[:190], show_alert=True)
    text, markup = await user_card(uid)
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("a:hidetop:"))
async def cb_hide_top(cb: CallbackQuery) -> None:
    """Вручную убрать человека из всех топов (или вернуть)."""
    uid = int(cb.data.split(":")[2])
    u = await services.get_user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    await services.set_hide_top(uid, not u.hide_top)
    await cb.answer("🙈 Убран из топов" if not u.hide_top else "👁 Возвращён в топы")
    text, markup = await user_card(uid)
    await show(cb, text, markup)


def _ids_from(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d{5,}", text or "")]


@router.message(Command("banids"))
async def cmd_banids(message: Message, bot: Bot) -> None:
    """/banids 123 456 … — массовый бан (с авто-чисткой заявок и данных)."""
    ids = _ids_from(message.text.split(maxsplit=1)[1] if " " in (message.text or "") else "")
    if not ids:
        await message.answer("Использование: <code>/banids 123456789 987654321 …</code>")
        return
    lines = []
    for uid in dict.fromkeys(ids):
        u = await services.get_user(uid)
        if not u:
            lines.append(f"❔ <code>{uid}</code> — в базе нет")
        elif uid in config.ADMIN_IDS:
            lines.append(f"⛔ <code>{uid}</code> — админ, пропущен")
        elif u.is_banned:
            lines.append(f"✔️ {nick(u)} · <code>{uid}</code> — уже в бане")
        else:
            rep = await moderation.ban_user(bot, uid, "массовый бан админом", by=message.from_user.id)
            lines.append(f"🚫 {nick(u)} · <code>{uid}</code> — {esc(moderation.report_text(rep))}")
    await message.answer("<b>Результат:</b>\n" + "\n".join(lines))


@router.message(Command("hidetop"))
async def cmd_hidetop(message: Message) -> None:
    """/hidetop 123 456 … — убрать из топов (повторно — вернуть)."""
    ids = _ids_from(message.text.split(maxsplit=1)[1] if " " in (message.text or "") else "")
    if not ids:
        await message.answer("Использование: <code>/hidetop 123456789</code> (повтор — вернуть в топ)")
        return
    lines = []
    for uid in dict.fromkeys(ids):
        u = await services.get_user(uid)
        if not u:
            lines.append(f"❔ <code>{uid}</code> — в базе нет")
            continue
        await services.set_hide_top(uid, not u.hide_top)
        lines.append(f"{'👁 возвращён' if u.hide_top else '🙈 убран'}: {nick(u)} · <code>{uid}</code>")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("a:trust:"))
async def cb_trust(cb: CallbackQuery) -> None:
    """«Это не фрод»: сбрасываем очки и больше не считаем подозрительным (награды и вывод размораживаются)."""
    uid = int(cb.data.split(":")[2])
    await fraud.trust(uid)
    await cb.answer("✅ Отмечен как доверенный, заморозка снята", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


@router.message(Command("fraud"))
async def cmd_fraud(message: Message) -> None:
    """Список самых подозрительных пользователей."""
    rows = await fraud.top_suspicious(15)
    if not rows:
        await message.answer("🛡 Подозрительных пользователей нет.")
        return
    lines = []
    for uid, pts in rows:
        u = await services.get_user(uid)
        flag = "🚫" if u and u.is_banned else ("🧊" if pts >= config.FRAUD_HOLD else "👀")
        lines.append(f"{flag} {nick(u) if u else uid} · <code>{uid}</code> · <b>{pts}</b>")
    await message.answer(
        "🛡 <b>Анти-фрод: подозрительные</b>\n"
        f"(🧊 заморозка ≥ {config.FRAUD_HOLD}, 🚫 автобан ≥ {config.FRAUD_BAN})\n\n" + "\n".join(lines)
        + "\n\nПрофиль: найти по ID в админке.",
    )


@router.callback_query(F.data.startswith("a:wdban:"))
async def cb_wd_ban(cb: CallbackQuery, bot: Bot) -> None:
    """Бан/разбан прямо из заявки на вывод (без захода в профиль). Сообщение остаётся на месте."""
    uid = int(cb.data.split(":")[2])
    u = await services.get_user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    if uid in config.ADMIN_IDS:
        await cb.answer("Нельзя банить администратора", show_alert=True)
        return
    banned = not u.is_banned
    if banned:
        report = await moderation.ban_user(bot, uid, "бан из заявки на вывод", by=cb.from_user.id)
        await cb.answer(f"🚫 Забанен. {moderation.report_text(report)}"[:190], show_alert=True)
    else:
        await moderation.unban_user(uid)
        await cb.answer("✅ Пользователь разбанен")
    # обновляем только кнопку бана в этом сообщении, остальные кнопки не трогаем
    markup = cb.message.reply_markup
    if markup:
        rows = []
        for row in markup.inline_keyboard:
            new_row = []
            for b in row:
                if b.callback_data == cb.data:
                    b = kb.B("✅ Разбанить" if banned else "🚫 Забанить", cb.data, style=kb.G if banned else kb.R)
                new_row.append(b)
            rows.append(new_row)
        try:
            await cb.message.edit_reply_markup(reply_markup=kb.Kb(inline_keyboard=rows))
        except TelegramAPIError:
            pass


# ───────────── списание звёзд ─────────────
@router.callback_query(F.data.startswith("a:deduct:"))
async def cb_deduct(cb: CallbackQuery, state: FSMContext) -> None:
    uid = int(cb.data.split(":")[2])
    u = await services.get_user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    await state.set_state(S.deduct)
    await state.update_data(uid=uid)
    await cb.answer()
    await cb.message.answer(
        f"Сколько звёзд списать у {nick(u)}?\nНа балансе: <b>{fmt_stars(u.balance)} ⭐</b>\n\nВведи число:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(S.deduct)
async def msg_deduct(message: Message, state: FSMContext, bot: Bot) -> None:
    cents = parse_stars(message.text or "")
    if cents is None:
        await message.answer("Введи положительное число, например 15 или 0.49", reply_markup=kb.cancel_kb())
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    done = await services.deduct_balance(uid, cents, message.from_user.id)
    if done is None:
        await message.answer("Пользователь не найден.")
        return
    if done == 0:
        await message.answer("На балансе нечего списывать.")
    else:
        note = "" if done == cents else f" (запрошено {short(cents)}, списано всё, что было на балансе)"
        await message.answer(f"✅ Списано {short(done)} ⭐{note}")
        try:
            await bot.send_message(uid, f"С вашего баланса списано {short(done)} звёзд")
        except TelegramAPIError:
            await message.answer("⚠️ Уведомление пользователю не доставлено (он заблокировал бота).")
    text, markup = await user_card(uid)
    await message.answer(text, reply_markup=markup)


# ───────────── вспомогательное: канал/чат по вводу админа ─────────────
async def resolve_chat(bot: Bot, raw: str):
    """-> (chat, error). Проверяет, что бот — администратор (иначе проверка подписки не работает)."""
    raw = (raw or "").strip()
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", raw)
    if m:
        raw = "@" + m.group(1)
    if re.fullmatch(r"-?\d+", raw):
        ref = int(raw)
    elif raw.startswith("@"):
        ref = raw
    else:
        return None, "Отправь @username канала или его числовой ID (-100…). Ссылки-приглашения сюда не подходят."
    try:
        chat = await bot.get_chat(ref)
    except TelegramAPIError as e:
        return None, f"Не удалось найти чат: {esc(e)}"
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except TelegramAPIError:
        return None, "Бот не состоит в этом чате. Добавь бота в канал администратором."
    if me.status not in ("administrator", "creator"):
        return None, "Сделай бота администратором канала — иначе он не сможет проверять подписку."
    return chat, None


# ───────────── задания ─────────────
@router.callback_query(F.data == "a:tasks")
async def cb_tasks(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    rows = await services.list_tasks()
    buttons = [
        [Btn(text=f"{'✅' if t.is_active else '⏸'} #{t.id} {t.title[:30]} · {n}", callback_data=f"a:task:{t.id}")]
        for t, n in rows[:30]
    ]
    buttons.append([Btn(text="➕ Добавить задание", callback_data="a:task_add")])
    buttons.append([Btn(text="⬅️ Назад", callback_data="a:menu")])
    await cb.answer()
    await show(
        cb,
        f"📝 <b>Задания</b> ({len(rows)})\n✅ — активно, ⏸ — выключено, число — сколько раз выполнено.",
        Kb(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("a:task:"))
async def cb_task_view(cb: CallbackQuery) -> None:
    await _render_task(cb, int(cb.data.split(":")[2]))


async def _render_task(cb: CallbackQuery, tid: int) -> None:
    t = await services.get_task(tid)
    if not t:
        await cb.answer("Не найдено", show_alert=True)
        return
    await cb.answer()
    await show(
        cb,
        f"📋 <b>#{t.id} {esc(t.title)}</b>\n"
        f"Ссылка: {esc(t.url)}\n"
        f"Автопроверка: {'да, чат ' + str(t.chat_id) if t.chat_id else 'нет'}\n"
        f"Награда: {short(t.reward) + ' ⭐' if t.reward else 'стандартная (' + short(settings.task_reward()) + ' ⭐)'}\n"
        f"Статус: {'активно' if t.is_active else 'выключено'}",
        Kb(
            inline_keyboard=[
                [
                    Btn(text="⏸ Выключить" if t.is_active else "▶️ Включить", callback_data=f"a:ttoggle:{t.id}"),
                    Btn(text="🗑 Удалить", callback_data=f"a:tdel:{t.id}"),
                ],
                [Btn(text="⬅️ Назад", callback_data="a:tasks")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:ttoggle:"))
async def cb_task_toggle(cb: CallbackQuery) -> None:
    tid = int(cb.data.split(":")[2])
    await services.toggle_task(tid)
    await _render_task(cb, tid)


@router.callback_query(F.data.startswith("a:tdel:"))
async def cb_task_del(cb: CallbackQuery, state: FSMContext) -> None:
    await services.delete_task(int(cb.data.split(":")[2]))
    await cb.answer("Удалено")
    await cb_tasks(cb, state)


@router.callback_query(F.data == "a:task_add")
async def cb_task_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.task_title)
    await cb.answer()
    await show(cb, "➕ <b>Новое задание</b>\n\nШаг 1/4. Введи название (например: «Подпишись на канал X»):", kb.cancel_kb())


@router.message(S.task_title)
async def msg_task_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 200:
        await message.answer("Название — от 1 до 200 символов.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(title=title)
    await state.set_state(S.task_url)
    await message.answer("Шаг 2/4. Отправь ссылку, по которой нужно перейти (https://…):", reply_markup=kb.cancel_kb())


@router.message(S.task_url)
async def msg_task_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not re.match(r"^https?://\S+$", url) or len(url) > 500:
        await message.answer("Нужна ссылка вида https://…", reply_markup=kb.cancel_kb())
        return
    await state.update_data(url=url)
    await state.set_state(S.task_chat)
    await message.answer(
        "Шаг 3/4. Это подписка на канал/чат, которую бот может проверить?\n"
        "Отправь @username или ID канала (бот должен быть там админом) — тогда проверка автоматическая.\n"
        "Если проверить нельзя — отправь <code>-</code>: награда будет выдаваться по кнопке «Проверить» "
        f"(не раньше чем через {10} секунд после показа задания).",
        reply_markup=kb.cancel_kb(),
    )


@router.message(S.task_chat)
async def msg_task_chat(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    chat_id = None
    if raw != "-":
        chat, err = await resolve_chat(bot, raw)
        if err:
            await message.answer(err + "\n\nИсправь и отправь снова, либо «-» без автопроверки.", reply_markup=kb.cancel_kb())
            return
        chat_id = chat.id
    await state.update_data(chat_id=chat_id)
    await state.set_state(S.task_price)
    await message.answer(
        "Шаг 4/4. <b>Какую цену поставить за выполнение задания?</b>\n"
        f"Стандартная сейчас — <b>{short(settings.task_reward())} ⭐</b>.",
        reply_markup=Kb(inline_keyboard=[
            [Btn(text="✅ Оставить стандартную", callback_data="a:tprice:std"),
             Btn(text="✏️ Ввести свою", callback_data="a:tprice:own")],
            [Btn(text="✖️ Отмена", callback_data="a:menu")],
        ]),
    )


async def _finish_task(target: Message, state: FSMContext, reward: int | None) -> None:
    data = await state.get_data()
    await state.clear()
    task = await services.add_task(data["title"], data["url"], data.get("chat_id"), reward)
    price = f"{short(reward)} ⭐" if reward else f"стандартная ({short(settings.task_reward())} ⭐)"
    await target.answer(
        f"✅ Задание #{task.id} добавлено ({'с автопроверкой' if data.get('chat_id') else 'без автопроверки'}).\n"
        f"💰 Награда: <b>{price}</b>",
        reply_markup=Kb(inline_keyboard=[[Btn(text="📝 К заданиям", callback_data="a:tasks")]]),
    )


@router.callback_query(S.task_price, F.data == "a:tprice:std")
async def cb_task_price_std(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _finish_task(cb.message, state, None)


@router.callback_query(S.task_price, F.data == "a:tprice:own")
async def cb_task_price_own(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(
        "Отправь, сколько ⭐ получит пользователь за это задание — например <code>0.8</code> или <code>2</code>:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(S.task_price)
async def msg_task_price(message: Message, state: FSMContext) -> None:
    cents = parse_stars(message.text or "")
    if not cents:
        await message.answer("Нужно положительное число, например <code>0.8</code>. Или нажми кнопку «Оставить стандартную».",
                             reply_markup=kb.cancel_kb())
        return
    await _finish_task(message, state, cents)


# ───────────── каналы обязательной подписки ─────────────
@router.callback_query(F.data == "a:channels")
async def cb_channels(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    channels = await services.list_channels()
    counts = await services.channel_counts()
    buttons = [
        [Btn(text=f"{'🔗' if c.is_link else ('🔎' if c.check_enabled else '⏳')} {c.title} ({counts.get(c.id, 0)})", callback_data=f"a:ch:{c.id}")]
        for c in channels
    ]
    buttons.append([Btn(text="➕ Добавить канал или ссылку", callback_data="a:ch_add")])
    buttons.append([Btn(text="⬅️ Назад", callback_data="a:menu")])
    await cb.answer()
    await show(
        cb,
        f"📢 <b>Каналы обязательной подписки</b> ({len(channels)})\n"
        "Пока человек не подпишется на все — бот не работает.\n\n"
        "🔎 — канал, бот проверяет подписку; ⏳ — канал без проверки; 🔗 — обычная ссылка (без проверки). "
        "Без проверки: после «Я подписался» бот ждёт 10 секунд.\n"
        "В скобках — сколько человек подписались/выполнили условие через этого бота.",
        Kb(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("a:ch:"))
async def cb_channel_view(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    ch = next((c for c in await services.list_channels() if c.id == cid), None)
    if not ch:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.answer()
    await show(
        cb,
        f"{'🔗' if ch.is_link else '📢'} <b>{esc(ch.title)}</b>\n"
        + ("🔗 Ссылка: проверить нельзя, после «Я подписался» бот ждёт 10 секунд\n" if ch.is_link else f"🔎 Подписка проверяется ботом\nID: <code>{ch.chat_id}</code>\n" if ch.check_enabled else "⏳ Без проверки: после «Я подписался» бот ждёт 10 секунд\n")
        + f"👥 Подписалось через бота: <b>{(await services.channel_counts()).get(ch.id, 0)}</b>\n"
        + f"{esc(ch.url)}",
        Kb(
            inline_keyboard=[
                [Btn(text="🗑 Удалить", callback_data=f"a:chdel:{ch.id}")],
                [Btn(text="⬅️ Назад", callback_data="a:channels")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:chdel:"))
async def cb_channel_del(cb: CallbackQuery, state: FSMContext) -> None:
    await services.delete_channel(int(cb.data.split(":")[2]))
    await cb.answer("Удалено")
    await cb_channels(cb, state)


@router.callback_query(F.data == "a:ch_add")
async def cb_channel_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show(
        cb,
        "➕ <b>Что добавить?</b>\n\n"
        "📢 <b>Канал</b> — можно проверять подписку (бот должен быть админом канала) или не проверять.\n\n"
        f"🔗 <b>Ссылка</b> — любая: сайт, бот, соцсеть, приглашение. Проверить её нельзя, поэтому всегда без проверки: "
        f"после «Я подписался» бот ждёт {NO_CHECK_WAIT} секунд.",
        Kb(
            inline_keyboard=[
                [Btn(text="📢 Канал", callback_data="a:ch_kind:channel", style="success")],
                [Btn(text="🔗 Ссылка", callback_data="a:ch_kind:link", style="primary")],
                [Btn(text="✖️ Отмена", callback_data="a:channels", style="danger")],
            ]
        ),
    )


@router.callback_query(F.data == "a:ch_kind:channel")
async def cb_channel_kind_channel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show(
        cb,
        "📢 <b>Новый канал</b>\n\n<b>Проверять подписку на этот канал?</b>\n\n"
        "🔎 <b>Да</b> — бот сам узнаёт, подписан ли человек, и не пускает без подписки. "
        "Для этого бот должен быть <b>администратором</b> канала.\n\n"
        f"⏳ <b>Нет</b> — после нажатия «Я подписался» бот просто ждёт {NO_CHECK_WAIT} секунд и пускает дальше. "
        "Подходит для каналов с заявками на вступление, приватных и тех, где бота нельзя сделать админом.",
        Kb(
            inline_keyboard=[
                [Btn(text="🔎 Да, проверять", callback_data="a:ch_mode:check", style="success")],
                [Btn(text=f"⏳ Нет, ждать {NO_CHECK_WAIT} сек.", callback_data="a:ch_mode:wait", style="primary")],
                [Btn(text="⬅️ Назад", callback_data="a:ch_add")],
            ]
        ),
    )


@router.callback_query(F.data == "a:ch_kind:link")
async def cb_channel_kind_link(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.lnk_url)
    await cb.answer()
    await show(
        cb,
        "🔗 <b>Новая ссылка</b>\n\nОтправь ссылку (https://…) — сайт, бот, соцсеть, приглашение. "
        f"Подписку на неё проверить нельзя: после «Я подписался» бот будет ждать {NO_CHECK_WAIT} секунд.",
        kb.cancel_kb(),
    )


def normalize_any_link(text: str) -> str | None:
    text = (text or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{4,32}", text):
        return "https://t.me/" + text[1:]
    if re.match(r"^(tg://\S+|https?://\S+\.\S+)$", text) and len(text) <= 500:
        return text
    if re.fullmatch(r"(?:t\.me|[A-Za-z0-9\-]+\.[A-Za-z]{2,})/\S*", text):
        return "https://" + text
    return None


@router.message(S.lnk_url)
async def msg_link_url(message: Message, state: FSMContext) -> None:
    url = normalize_any_link(message.text or "")
    if not url:
        await message.answer("Не похоже на ссылку. Пример: <code>https://example.com</code> или <code>https://t.me/mybot</code>",
                             reply_markup=kb.cancel_kb())
        return
    await state.update_data(url=url)
    await state.set_state(S.lnk_title)
    await message.answer("Как назвать ссылку на кнопке для пользователей? (до 60 символов)", reply_markup=kb.cancel_kb())


@router.message(S.lnk_title)
async def msg_link_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 60:
        await message.answer("Название — от 1 до 60 символов.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    ok = await services.add_channel(None, title, data["url"], check_enabled=False, is_link=True)
    await state.clear()
    await message.answer(
        "✅ Ссылка добавлена." if ok else "Такая ссылка уже добавлена.",
        reply_markup=Kb(inline_keyboard=[[Btn(text="📢 К списку", callback_data="a:channels")]]),
    )


@router.callback_query(F.data == "a:ch_mode:check")
async def cb_channel_mode_check(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.ch_chat)
    await cb.answer()
    await show(
        cb,
        "🔎 <b>Канал с проверкой</b>\n\nСначала сделай бота <b>администратором</b> канала.\n"
        "Затем отправь @username или числовой ID канала (-100…):",
        kb.cancel_kb(),
    )


@router.callback_query(F.data == "a:ch_mode:wait")
async def cb_channel_mode_wait(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.ch_link)
    await cb.answer()
    await show(
        cb,
        "⏳ <b>Канал без проверки</b>\n\nОтправь ссылку на канал: @username, https://t.me/username "
        "или ссылку-приглашение https://t.me/+…",
        kb.cancel_kb(),
    )


def normalize_channel_link(text: str) -> tuple[str | None, str | None]:
    """-> (url, публичный @username или None). url=None — формат не подошёл."""
    text = (text or "").strip()
    m = re.fullmatch(r"@([A-Za-z0-9_]{4,32})", text) or re.fullmatch(
        r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,32})/?", text
    )
    if m:
        return f"https://t.me/{m.group(1)}", m.group(1)
    if re.fullmatch(r"(?:https?://)?t\.me/(?:\+|joinchat/)[A-Za-z0-9_\-]+/?", text):
        return ("https://" + text.split("://")[-1]), None
    return None, None


async def finish_channel(message: Message, state: FSMContext, chat_id: int | None, title: str, url: str, check: bool) -> None:
    ok = await services.add_channel(chat_id, title, url, check_enabled=check)
    await state.clear()
    await message.answer(
        "✅ Канал добавлен." if ok else "Этот канал уже добавлен.",
        reply_markup=Kb(inline_keyboard=[[Btn(text="📢 К каналам", callback_data="a:channels")]]),
    )


@router.message(S.ch_link)
async def msg_channel_link(message: Message, state: FSMContext, bot: Bot) -> None:
    url, username = normalize_channel_link(message.text or "")
    if not url:
        await message.answer(
            "Не похоже на ссылку на канал. Пример: <code>@mychannel</code>, <code>https://t.me/mychannel</code> "
            "или <code>https://t.me/+AbCdEf…</code>",
            reply_markup=kb.cancel_kb(),
        )
        return
    if username:  # публичный канал: название и ID бот может узнать и без прав админа
        try:
            chat = await bot.get_chat("@" + username)
        except TelegramAPIError:
            chat = None
        if chat:
            await finish_channel(message, state, chat.id, chat.title or username, url, check=False)
            return
    await state.update_data(url=url, chat_id=None)
    await state.set_state(S.ch_title)
    await message.answer("Как назвать канал на кнопке для пользователей? (до 60 символов)", reply_markup=kb.cancel_kb())


@router.message(S.ch_title)
async def msg_channel_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 60:
        await message.answer("Название — от 1 до 60 символов.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    await finish_channel(message, state, data.get("chat_id"), title, data["url"], check=False)


@router.message(S.ch_chat)
async def msg_channel_chat(message: Message, state: FSMContext, bot: Bot) -> None:
    chat, err = await resolve_chat(bot, message.text or "")
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    if chat.username:
        ok = await services.add_channel(chat.id, chat.title or chat.username, f"https://t.me/{chat.username}")
        await state.clear()
        await message.answer(
            "✅ Канал добавлен." if ok else "Этот канал уже добавлен.",
            reply_markup=Kb(inline_keyboard=[[Btn(text="📢 К каналам", callback_data="a:channels")]]),
        )
        return
    await state.update_data(chat_id=chat.id, title=chat.title or str(chat.id))
    await state.set_state(S.ch_url)
    await message.answer("Канал приватный. Отправь ссылку-приглашение (https://t.me/+…):", reply_markup=kb.cancel_kb())


@router.message(S.ch_url)
async def msg_channel_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not re.match(r"^https://t\.me/\S+$", url):
        await message.answer("Нужна ссылка вида https://t.me/…", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    await state.clear()
    ok = await services.add_channel(data["chat_id"], data["title"], url)
    await message.answer(
        "✅ Канал добавлен." if ok else "Этот канал уже добавлен.",
        reply_markup=Kb(inline_keyboard=[[Btn(text="📢 К каналам", callback_data="a:channels")]]),
    )


# ───────────── рассылка ─────────────
@router.callback_query(F.data == "a:broadcast")
async def cb_broadcast(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    counts = await services.broadcast_counts()
    await cb.answer()
    await show(
        cb,
        f"✉️ <b>Рассылка</b>\n{LINE}\n\nКому отправляем? В скобках — сколько получателей "
        "(заблокировавшие бота и забаненные не учитываются).",
        kb.broadcast_categories(counts),
    )


@router.callback_query(F.data.startswith("a:bcat:"))
async def cb_broadcast_category(cb: CallbackQuery, state: FSMContext) -> None:
    category = cb.data.split(":")[2]
    if category not in services.BROADCAST_CATEGORIES:
        await cb.answer()
        return
    await state.set_state(S.broadcast)
    await state.update_data(category=category)
    await cb.answer()
    await show(
        cb,
        f"✉️ <b>Рассылка: {services.BROADCAST_CATEGORIES[category]}</b>\n\n"
        "Отправь сообщение, которое получат пользователи "
        "(текст, фото, видео — как есть, с форматированием и кнопками-ссылками).",
        kb.cancel_kb(),
    )


@router.message(S.broadcast)
async def msg_broadcast(message: Message, state: FSMContext) -> None:
    category = (await state.get_data()).get("category", "all")
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    n = len(await services.broadcast_ids(category))
    await message.answer(
        f"Разослать это сообщение <b>{n}</b> пользователям\n"
        f"({services.BROADCAST_CATEGORIES[category]})?\n\nСообщение выше — как оно выглядит.",
        reply_markup=kb.confirm_broadcast_kb(),
        reply_to_message_id=message.message_id,
    )


_broadcast_running = False


@router.callback_query(F.data == "a:bc_go")
async def cb_broadcast_go(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    global _broadcast_running
    data = await state.get_data()
    if "message_id" not in data:
        await cb.answer("Нет сообщения для рассылки", show_alert=True)
        return
    if _broadcast_running:
        await cb.answer("Другая рассылка ещё идёт", show_alert=True)
        return
    await state.clear()
    await cb.answer("Рассылка запущена")
    await show(cb, "🚀 Рассылка запущена. Пришлю отчёт, когда закончится.", kb.admin_back())
    task = asyncio.create_task(
        _run_broadcast(bot, cb.from_user.id, data["chat_id"], data["message_id"], data.get("category", "all"))
    )
    _bg_tasks.add(task)  # иначе сборщик мусора может убить задачу
    task.add_done_callback(_bg_tasks.discard)


_bg_tasks: set[asyncio.Task] = set()


async def _run_broadcast(bot: Bot, admin_id: int, from_chat: int, message_id: int, category: str) -> None:
    global _broadcast_running
    _broadcast_running = True
    ok = blocked = failed = 0
    try:
        for uid in await services.broadcast_ids(category):
            for _attempt in range(3):
                try:
                    await bot.copy_message(uid, from_chat, message_id)
                    ok += 1
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramForbiddenError:
                    blocked += 1
                    await services.set_blocked(uid, True)
                except TelegramAPIError as e:
                    failed += 1
                    log.warning("broadcast to %s failed: %s", uid, e)
                break
            await asyncio.sleep(0.05)  # ~20 сообщений в секунду
    finally:
        _broadcast_running = False
    try:
        await bot.send_message(
            admin_id,
            f"✅ Рассылка завершена\n\nДоставлено: <b>{ok}</b>\nЗаблокировали бота: {blocked}\nОшибок: {failed}",
        )
    except TelegramAPIError:
        pass


# ───────────── начисление звёзд ─────────────
@router.callback_query(F.data.startswith("a:bonus:"))
async def cb_bonus(cb: CallbackQuery, state: FSMContext) -> None:
    uid = int(cb.data.split(":")[2])
    u = await services.get_user(uid)
    if not u:
        await cb.answer("Не найден", show_alert=True)
        return
    await state.set_state(S.bonus)
    await state.update_data(uid=uid)
    await cb.answer()
    await cb.message.answer(
        f"Сколько звёзд начислить {nick(u)}?\nСейчас на балансе: <b>{fmt_stars(u.balance)} ⭐</b>\n\nВведи число:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(S.bonus)
async def msg_bonus(message: Message, state: FSMContext, bot: Bot) -> None:
    cents = parse_stars(message.text or "")
    if cents is None:
        await message.answer("Введи положительное число, например 5 или 0.49", reply_markup=kb.cancel_kb())
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    if not await services.add_bonus(uid, cents, message.from_user.id):
        await message.answer("Пользователь не найден.")
        return
    await message.answer(f"✅ Начислено {short(cents)} ⭐")
    try:
        await bot.send_message(uid, f"🎁 Вам начислено <b>{short(cents)} ⭐</b>")
    except TelegramAPIError:
        await message.answer("⚠️ Уведомление пользователю не доставлено (он заблокировал бота).")
    text, markup = await user_card(uid)
    await message.answer(text, reply_markup=markup)


# ───────────── личное сообщение пользователю ─────────────
@router.callback_query(F.data.startswith("a:dm:"))
async def cb_dm(cb: CallbackQuery, state: FSMContext) -> None:
    uid = int(cb.data.split(":")[2])
    await state.set_state(S.dm)
    await state.update_data(uid=uid)
    await cb.answer()
    await cb.message.answer(
        f"Отправь сообщение для <code>{uid}</code> (текст, фото, видео — как есть):",
        reply_markup=kb.cancel_kb(),
    )


@router.message(S.dm)
async def msg_dm(message: Message, state: FSMContext, bot: Bot) -> None:
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await bot.copy_message(uid, message.chat.id, message.message_id)
        await message.answer("✅ Сообщение доставлено.")
    except TelegramForbiddenError:
        await services.set_blocked(uid, True)
        await message.answer("⚠️ Пользователь заблокировал бота.")
    except TelegramAPIError as e:
        await message.answer(f"⚠️ Не доставлено: {esc(e)}")
    text, markup = await user_card(uid)
    await message.answer(text, reply_markup=markup)


# ───────────── настройки ─────────────
@router.callback_query(F.data == "a:settings")
async def cb_settings(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show(
        cb,
        f"⚙️ <b>Настройки</b>\n{LINE}\n\nНажми на параметр, чтобы изменить. Новые значения "
        "действуют сразу, перезапуск не нужен.",
        kb.settings_kb(),
    )


HINTS = {
    "task_reward": "число звёзд, например <code>0.49</code>",
    "ref_reward": "число звёзд, например <code>11</code>",
    "ref_tasks": "целое число заданий, например <code>5</code>",
    "task_rate": "целое число заданий в минуту, например <code>2</code>",
    "withdraw_amounts": "суммы через запятую, например <code>15, 25, 50, 100</code>",
    "withdraw_term": "текст, например <code>до 48 часов</code>",
}


@router.callback_query(F.data.startswith("a:set:"))
async def cb_setting_edit(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":")[2]
    if key not in kb.SETTING_TITLES:
        await cb.answer()
        return
    await state.set_state(S.setting)
    await state.update_data(key=key)
    await cb.answer()
    await show(
        cb,
        f"{kb.SETTING_TITLES[key]}\nСейчас: <b>{esc(kb.setting_display(key))}</b>\n\n"
        f"Отправь новое значение — {HINTS[key]}:",
        kb.setting_edit_kb(key),
    )


def parse_setting(key: str, text: str) -> str | None:
    """Возвращает значение для хранения или None, если ввод некорректен."""
    text = (text or "").strip()
    if key in ("task_reward", "ref_reward"):
        cents = parse_stars(text)
        return str(cents) if cents else None
    if key == "ref_tasks":
        return str(int(text)) if text.isdigit() and 1 <= int(text) <= 100 else None
    if key == "task_rate":
        return str(int(text)) if text.isdigit() and 1 <= int(text) <= 60 else None
    if key == "withdraw_amounts":
        parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
        if not parts or len(parts) > 8 or not all(p.isdigit() and 1 <= int(p) <= 100000 for p in parts):
            return None
        return ",".join(str(x) for x in sorted({int(p) for p in parts}))
    return text if 1 <= len(text) <= 100 else None  # withdraw_term


@router.message(S.setting)
async def msg_setting(message: Message, state: FSMContext) -> None:
    key = (await state.get_data())["key"]
    value = parse_setting(key, message.text or "")
    if value is None:
        await message.answer(f"Не подходит. Нужно: {HINTS[key]}", reply_markup=kb.setting_edit_kb(key))
        return
    await settings.set_value(key, value)
    await state.clear()
    await message.answer(f"✅ Сохранено: {kb.SETTING_TITLES[key]} → <b>{esc(kb.setting_display(key))}</b>", reply_markup=kb.settings_kb())


@router.callback_query(F.data.startswith("a:setreset:"))
async def cb_setting_reset(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":")[2]
    if key in kb.SETTING_TITLES:
        await settings.reset(key)
    await state.clear()
    await cb.answer("Возвращено значение по умолчанию")
    await cb_settings(cb, state)


# ───────────── топы ─────────────
TOP_TITLES = {"balance": "💰 Топ по балансу", "earned": "🏆 Топ по заработку", "refs": "👥 Топ по рефералам (засчитанные)"}


@router.callback_query(F.data.startswith("a:top:"))
async def cb_admin_top(cb: CallbackQuery) -> None:
    field = cb.data.split(":")[2]
    if field not in TOP_TITLES:
        await cb.answer()
        return
    rows = await services.top_by(field, limit=15)
    text = f"<b>{TOP_TITLES[field]}</b>\n{LINE}\n\n"
    if not rows:
        text += "Пока пусто."
    for i, (u, value) in enumerate(rows, 1):
        shown = value if field == "refs" else f"{fmt_stars(value)} ⭐"
        text += f"{i}. {nick(u)} · <code>{u.id}</code> — <b>{shown}</b>\n"
    await cb.answer()
    await show(cb, text, kb.admin_tops(field))


# ───────────── экспорт ─────────────
@router.callback_query(F.data == "a:export")
async def cb_export(cb: CallbackQuery) -> None:
    await cb.answer("Готовлю файл…")
    rows = await services.export_users()
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    else:
        buf.write("id\n")
    data = buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel открыл кириллицу
    await cb.message.answer_document(
        BufferedInputFile(data, filename="users.csv"),
        caption=f"📤 Пользователей в выгрузке: {len(rows)}",
    )


# ───────────── заявки на вывод ─────────────
def wd_detail_text(w, user, closer: str | None = None) -> str:
    return notify.request_text(w, user, closer)


@router.callback_query(F.data == "a:wd_list")
async def cb_wd_list(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    items = await services.list_withdrawals("pending", limit=30)
    await cb.answer()
    text = f"💫 <b>Заявки на вывод</b>\n{LINE}\n\n"
    text += f"⏳ Ожидают обработки: <b>{len(items)}</b>" if items else "Новых заявок нет 👌"
    await show(cb, text, kb.wd_list_kb(items))


@router.callback_query(F.data == "a:wd_hist")
async def cb_wd_hist(cb: CallbackQuery) -> None:
    items = await services.list_withdrawals("closed", limit=20)
    await cb.answer()
    text = f"🕘 <b>История заявок</b>\n{LINE}\n\nПоследние закрытые и отклонённые:" if items else "История пуста."
    await show(cb, text, kb.wd_list_kb(items, back_to="a:wd_list", history=True))


async def _closer_name(admin_id: int) -> str:
    u = await services.get_user(admin_id)
    return mention(u) if u else str(admin_id)


@router.callback_query(F.data.startswith("a:wd:"))
async def cb_wd_view(cb: CallbackQuery) -> None:
    await _render_withdrawal(cb, int(cb.data.split(":")[2]))


async def _render_withdrawal(cb: CallbackQuery, wid: int) -> None:
    found = await services.get_withdrawal(wid)
    if not found:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    w, user = found
    closer = await _closer_name(w.closed_by) if w.closed_by else None
    await cb.answer()
    await show(cb, wd_detail_text(w, user, closer), kb.wd_detail_kb(w.id, user.id, w.status == "pending"))


@router.callback_query(F.data.startswith("a:wd_close:"))
async def cb_wd_close(cb: CallbackQuery, bot: Bot) -> None:
    wid = int(cb.data.split(":")[2])
    result, w = await services.close_withdrawal(wid, cb.from_user.id)
    if result == "missing":
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if result == "insufficient":
        user = await services.get_user(w.user_id)
        await cb.answer(
            f"На балансе {fmt_stars(user.balance)} ⭐ — меньше суммы заявки ({fmt_short(w.amount)} ⭐). "
            "Закрыть нельзя: отклони заявку или начисли пользователю звёзды.",
            show_alert=True,
        )
        return
    if result == "already":
        await cb.answer("Заявка уже обработана другим админом", show_alert=True)
        await _render_withdrawal(cb, wid)
        return

    user = await services.get_user(w.user_id)
    closer = await _closer_name(cb.from_user.id)
    await notify.refresh_notices(bot, w, user, closer)
    await cb.answer("✅ Заявка закрыта, звёзды списаны")
    await show(cb, wd_detail_text(w, user, closer), kb.wd_detail_kb(w.id, user.id, False))
    try:
        await bot.send_message(
            w.user_id,
            f"✅ <b>Заявка №{w.id} закрыта</b>\n\nС вашего баланса списано {fmt_short(w.amount)} звёзд",
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("a:wd_reject:"))
async def cb_wd_reject(cb: CallbackQuery, bot: Bot) -> None:
    wid = int(cb.data.split(":")[2])
    result, w = await services.reject_withdrawal(wid, cb.from_user.id)
    if result == "missing":
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if result == "already":
        await cb.answer("Заявка уже обработана другим админом", show_alert=True)
        await _render_withdrawal(cb, wid)
        return

    user = await services.get_user(w.user_id)
    closer = await _closer_name(cb.from_user.id)
    await notify.refresh_notices(bot, w, user, closer)
    await cb.answer("Заявка отклонена")
    await show(cb, wd_detail_text(w, user, closer), kb.wd_detail_kb(w.id, user.id, False))
    try:
        await bot.send_message(
            w.user_id,
            f"❌ <b>Заявка №{w.id} отклонена</b>\n\nЗвёзды с баланса не списаны. "
            "Если это ошибка — создай заявку заново или обратись к администратору.",
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("a:user:"))
async def cb_user_card(cb: CallbackQuery) -> None:
    card = await user_card(int(cb.data.split(":")[2]))
    if not card:
        await cb.answer("Не найден", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(card[0], reply_markup=card[1])
