from __future__ import annotations

import asyncio
import logging
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
import fraud
import keyboards as kb
import notify
import moderation
import services
import settings
import sponsors
from middlewares import PENDING_CHECKS, forget_sub_cache, is_member
from utils import _bold, bar, esc, fmt_short, fmt_stars, mention, public_name

log = logging.getLogger(__name__)
router = Router(name="user")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MIN_TASK_SECONDS = 10  # для заданий без автопроверки: не раньше, чем через N секунд после показа
_shown: dict[tuple[int, int], float] = {}
_net: dict[tuple[int, int], sponsors.Task] = {}  # (user, локальный task_id) -> задание рекламной сети

LINE = "━━━━━━━━━━━━━━━━━━"


def title(emoji: str, text: str) -> str:
    return f"{emoji} <b>{text}</b>\n{LINE}\n\n"


async def show(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует текущее сообщение, а если нельзя — отправляет новое."""
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


def menu_text(user) -> str:
    return (
        title("✨", "BLUD STARS")
        + "<i>Выполняй простые задания и получай звёзды Telegram</i>\n\n"
        + f"🎯 За задание — <b>{fmt_short(settings.task_reward())} ⭐</b>\n"
        + f"👥 За друга — <b>{fmt_short(settings.ref_reward())} ⭐</b>\n\n"
        + f"💰 Баланс: <b>{fmt_stars(user.balance)} ⭐</b>\n\n"
        + "👇 Выбери действие"
    )


class UserS(StatesGroup):
    check_password = State()


# ───────────── старт / меню ─────────────
@router.message(CommandStart())
async def cmd_start(message: Message, user, is_admin: bool, state: FSMContext, bot: Bot) -> None:
    await state.set_state(None)
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ""
    if payload.startswith("chk_"):
        PENDING_CHECKS.pop(user.id, None)  # дошли до обработчика — подписки в порядке, активируем сразу
        await process_check(bot, state, user, payload[4:], message.answer)
        return
    fresh = await services.get_user(user.id)
    await message.answer(menu_text(fresh), reply_markup=kb.main_menu(is_admin))


@router.callback_query(F.data.in_({"menu", "check_sub"}))
async def cb_menu(cb: CallbackQuery, user, is_admin: bool, state: FSMContext, bot: Bot) -> None:
    await state.set_state(None)
    if cb.data == "check_sub":
        forget_sub_cache(user.id)
    fresh = await services.get_user(user.id)
    try:  # после ожидания на стене подписки callback может быть уже «протухшим» — это не ошибка
        await cb.answer("✅ Подписка подтверждена" if cb.data == "check_sub" else None)
    except TelegramAPIError:
        pass
    await show(cb, menu_text(fresh), kb.main_menu(is_admin))
    if cb.data == "check_sub":
        code = PENDING_CHECKS.pop(user.id, None)
        if code:  # человек открыл чек до подписки — активируем теперь
            await process_check(bot, state, user, code, cb.message.answer)


# ───────────── чеки ─────────────
MENU_KB = InlineKeyboardMarkup(inline_keyboard=[[kb.B("🏠 В меню", "menu", style=kb.P)]])
CHECK_PROBLEMS = {
    "missing": "❌ Чек не найден. Проверь ссылку.",
    "inactive": "🚫 Этот чек отключён администратором.",
    "expired": "⌛ Срок действия этого чека истёк.",
    "exhausted": "😔 Этот чек уже полностью активирован — места закончились.",
    "already": "ℹ️ Ты уже активировал этот чек.",
}
_pw_fails: dict[tuple[int, str], list[float]] = {}
PW_TRIES, PW_WINDOW = 5, 600  # 5 неверных паролей за 10 минут — пауза


async def process_check(bot: Bot, state: FSMContext, user, code: str, reply) -> None:
    """Пароль -> подписки -> зачисление. reply(text, markup) — куда отвечать."""
    chk = await services.get_check_by_code(code)
    if not chk:
        await reply(CHECK_PROBLEMS["missing"], reply_markup=MENU_KB)
        return
    problem = services.check_problem(chk)
    if problem:
        await reply(CHECK_PROBLEMS[problem], reply_markup=MENU_KB)
        return
    if await services.has_activated(chk.id, user.id):
        await reply(CHECK_PROBLEMS["already"], reply_markup=MENU_KB)
        return

    if chk.password and code not in (await state.get_data()).get("chk_pw_ok", []):
        await state.set_state(UserS.check_password)
        await state.update_data(chk_code=code)
        await reply("🔐 <b>Этот чек защищён паролем.</b>\n\nОтправь пароль одним сообщением:", reply_markup=kb.back("menu", "✖️ Отмена"))
        return

    missing, unknown = [], False
    for c in services.check_channels(chk):
        member = await is_member(bot, c["chat_id"], user.id)
        if member is False:
            missing.append(c)
        elif member is None:
            unknown = True
    if missing:
        names = "\n".join(f"• {esc(c['title'])}" for c in missing)
        await reply(
            f"❌ <b>Ты не подписан на:</b>\n{names}\n\nПодпишись и нажми «Проверить подписку».",
            reply_markup=kb.check_subscribe_kb(missing, code),
        )
        return
    if unknown:
        log.error("чек %s: не удалось проверить подписку (бот не админ канала?)", code)
        await reply("⚠️ Сейчас не получается проверить подписку. Попробуй чуть позже или напиши администратору.", reply_markup=MENU_KB)
        return

    status, chk, amount = await services.activate_check(user.id, code)
    if status != "ok":
        await reply(CHECK_PROBLEMS[status], reply_markup=MENU_KB)
        return
    fresh = await services.get_user(user.id)
    await reply(
        title("🎉", "Чек активирован!")
        + f"На твой баланс зачислено <b>{fmt_short(amount)} ⭐</b>\n"
        f"💰 Баланс: <b>{fmt_stars(fresh.balance)} ⭐</b>",
        reply_markup=MENU_KB,
    )
    if chk.max_people is not None and chk.activations >= chk.max_people:
        try:
            await bot.send_message(chk.created_by, f"🧾 Чек №{chk.id} полностью активирован ({chk.activations}/{chk.max_people}).")
        except TelegramAPIError:
            pass


@router.callback_query(F.data.startswith("chk_go:"))
async def cb_check_go(cb: CallbackQuery, user, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    await process_check(bot, state, user, cb.data.split(":", 1)[1], cb.message.answer)


@router.message(UserS.check_password, F.text, ~F.text.startswith("/"))
async def msg_check_password(message: Message, user, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    code = data.get("chk_code")
    chk = await services.get_check_by_code(code) if code else None
    if not chk or not chk.password:
        await state.set_state(None)
        await message.answer(CHECK_PROBLEMS["missing"], reply_markup=MENU_KB)
        return

    now = time.time()
    fails = [t for t in _pw_fails.get((user.id, code), []) if now - t < PW_WINDOW]
    if len(fails) >= PW_TRIES:
        await state.set_state(None)
        await message.answer("⏳ Слишком много неверных попыток. Попробуй снова через 10 минут.", reply_markup=MENU_KB)
        return
    if not secrets.compare_digest(message.text.strip().encode(), chk.password.encode()):
        fails.append(now)
        _pw_fails[(user.id, code)] = fails
        await message.answer(
            f"❌ Неверный пароль. Осталось попыток: <b>{PW_TRIES - len(fails)}</b>",
            reply_markup=kb.back("menu", "✖️ Отмена"),
        )
        return

    _pw_fails.pop((user.id, code), None)
    await state.set_state(None)
    await state.update_data(chk_pw_ok=list({*data.get("chk_pw_ok", []), code}))
    await process_check(bot, state, user, code, message.answer)


# ───────────── задания ─────────────
@router.callback_query(F.data == "earn")
async def cb_earn(cb: CallbackQuery, user) -> None:
    fresh = await services.get_user(user.id)
    await cb.answer()
    await show(
        cb,
        title("⭐", "Заработать звёзды")
        + f"За каждое задание — <b>{fmt_short(settings.task_reward())} ⭐</b>\n\n"
        "1️⃣ Нажми «Выполнить задание»\n"
        "2️⃣ Перейди по ссылке и выполни условия\n"
        "3️⃣ Вернись и нажми «Проверить»\n\n"
        f"✅ Выполнено заданий: <b>{fresh.tasks_done}</b>\n"
        f"💰 Баланс: <b>{fmt_stars(fresh.balance)} ⭐</b>",
        kb.earn_menu(),
    )


def cooldown_text(wait: int) -> str:
    return (
        f"⏳ Не так быстро! Можно выполнять не более {settings.task_rate()} заданий в минуту.\n"
        f"Подожди {wait} сек. и нажми «Проверить» снова."
    )


async def net_next(u):
    """Следующее задание от рекламных сетей, которое этот человек ещё не делал и не пропускал."""
    if not sponsors.enabled():
        return None
    for nt in await sponsors.fetch(u, limit=10):
        local = await services.get_net_task(nt.provider, nt.title, nt.uid)
        if not await services.has_user_task(u.id, local.id):
            _net[(u.id, local.id)] = nt
            return local, nt
    return None


async def send_next_task(cb: CallbackQuery, user_id: int) -> None:
    """Свои задания админов и задания рекламных сетей чередуются; если одного вида нет — берём другой."""
    fresh = await services.get_user(user_id)
    own_first = ((fresh.tasks_done + fresh.tasks_skipped) % 2 == 0) if fresh else True
    task, desc, url = None, "", ""
    for kind in (("own", "net") if own_first else ("net", "own")):
        if kind == "own":
            task = await services.next_task(user_id)
            if task:
                desc, url = task.description, task.url
        else:
            found = await net_next(cb.from_user)
            if found:
                task, nt = found
                desc, url = "Подпишись или запусти — и награда твоя.", nt.link
        if task:
            break
    if not task:
        await show(
            cb,
            title("😔", "Заданий пока нет")
            + "Пока нет новых заданий для тебя — приходи позже!",
            kb.back("earn"),
        )
        return
    _shown[(user_id, task.id)] = time.time()
    text = title("📋", esc(task.title))
    if desc:
        text += f"{esc(desc)}\n\n"
    text += (
        "1️⃣ Нажми «Перейти к заданию»\n"
        "2️⃣ Выполни условия\n"
        "3️⃣ Вернись и нажми «Проверить»\n\n"
        f"💰 Награда: <b>{fmt_short(task.reward or settings.task_reward())} ⭐</b>\n"
        f"<i>⏱ Не более {settings.task_rate()} заданий в минуту</i>"
    )
    await show(cb, text, kb.task_kb(task.id, url))


@router.callback_query(F.data == "task_next")
async def cb_task_next(cb: CallbackQuery, user) -> None:
    await cb.answer()
    await send_next_task(cb, user.id)


@router.callback_query(F.data.startswith("t_check:"))
async def cb_task_check(cb: CallbackQuery, user, bot: Bot) -> None:
    task_id = int(cb.data.split(":")[1])
    task = await services.get_task(task_id)
    if not task or not task.is_active:
        await cb.answer("Это задание больше недоступно", show_alert=True)
        await send_next_task(cb, user.id)
        return

    wait = await services.task_cooldown(user.id)
    if wait:
        await cb.answer(cooldown_text(wait), show_alert=True)
        return

    net = None
    if task.source != "own":
        net = _net.get((user.id, task_id))
        if net is None:  # после перезапуска бота — восстанавливаем из выдачи сети
            net = next((n for n in await sponsors.fetch(cb.from_user, limit=10) if n.uid == task.url), None)
        if net is None:
            await cb.answer("Это задание уже недоступно", show_alert=True)
            await services.skip_task(user.id, task_id)
            await send_next_task(cb, user.id)
            return

    if net is not None:
        state = await sponsors.task_state(cb.from_user, net)
        if state == "gone":  # сеть сняла задание — не мучаем человека, берём следующее
            await cb.answer("Это задание уже снято, даю другое", show_alert=False)
            await services.skip_task(user.id, task_id)
            _net.pop((user.id, task_id), None)
            await send_next_task(cb, user.id)
            return
        if state == "error":  # сеть не ответила — это не вина человека
            await cb.answer("⚠️ Сеть заданий не ответила. Нажми «Проверить» ещё раз через пару секунд.", show_alert=True)
            return
        if state != "done":
            await cb.answer("❌ Условие не выполнено. Выполни задание и нажми «Проверить» ещё раз.", show_alert=True)
            return
    elif task.chat_id:
        member = await is_member(bot, task.chat_id, user.id)
        if member is False:
            await cb.answer("❌ Условие не выполнено. Подпишись и нажми «Проверить» ещё раз.", show_alert=True)
            return
        # member is None — бот не смог проверить (не админ канала): не лишаем человека награды
    else:
        shown = _shown.get((user.id, task_id))
        if shown is None:
            _shown[(user.id, task_id)] = time.time()
            await cb.answer("Сначала перейди по ссылке и выполни задание, затем нажми «Проверить».", show_alert=True)
            return
        if time.time() - shown < MIN_TASK_SECONDS:
            await cb.answer("⏳ Задание ещё не выполнено. Выполни его и нажми «Проверить» чуть позже.", show_alert=True)
            return

    async with _task_locks.setdefault(user.id, asyncio.Lock()):
        result = await services.complete_task(user.id, task_id)
    if result is not None and result.get("wait"):
        await cb.answer(cooldown_text(result["wait"]), show_alert=True)
        return
    if result is None:
        await cb.answer("Это задание уже засчитано", show_alert=True)
        await send_next_task(cb, user.id)
        return

    shown_at = _shown.pop((user.id, task_id), None)
    _net.pop((user.id, task_id), None)
    await cb.answer(f"✅ +{fmt_short(result['reward'])} ⭐ зачислено!", show_alert=False)

    # анти-фрод: слишком быстрая проверка, машинный ритм, активность сутками; набрал очков — бан / заморозка
    try:
        if shown_at is not None:
            await fraud.on_fast_click(user.id, time.time() - shown_at)
        await fraud.check_task_pattern(user.id, result["tasks_done"], settings.task_rate())
        await moderation.evaluate(bot, user.id)
        if user.referrer_id:
            await moderation.evaluate(bot, user.referrer_id)
    except Exception as e:  # noqa: BLE001
        log.warning("anti-fraud after task: %s", e)
    if (await services.get_user(user.id)).is_banned:
        return

    if result["referrer_id"]:
        try:
            await bot.send_message(
                result["referrer_id"],
                f"🎉 <b>Друг засчитан!</b>\n\nТвой реферал {mention(user)} выполнил {settings.ref_tasks()} заданий.\n"
                f"Тебе начислено <b>{fmt_short(result['ref_reward'])} ⭐</b>",
            )
        except TelegramAPIError:
            pass

    await send_next_task(cb, user.id)


@router.callback_query(F.data.startswith("t_skip:"))
async def cb_task_skip(cb: CallbackQuery, user) -> None:
    task_id = int(cb.data.split(":")[1])
    await services.skip_task(user.id, task_id)
    _shown.pop((user.id, task_id), None)
    await cb.answer("Задание пропущено")
    await send_next_task(cb, user.id)


# ───────────── рефералы ─────────────
@router.callback_query(F.data == "refs")
async def cb_refs(cb: CallbackQuery, user) -> None:
    total, counted = await services.referral_counts(user.id)
    fresh = await services.get_user(user.id)
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{user.id}"
    await cb.answer()
    await show(
        cb,
        title("👥", "Рефералы")
        + "Зарабатывай звёзды, приглашая друзей по своей реферальной ссылке 🔗\n\n"
        f"├ За приглашённого друга — <b>{fmt_short(settings.ref_reward())} ⭐</b>\n"
        "├ Друг должен перейти по твоей ссылке\n"
        f"└ И выполнить {settings.ref_tasks()} заданий от спонсоров\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👥 Всего приглашено: <b>{total}</b>\n"
        f"✅ Засчитано: <b>{counted}</b>\n"
        f"🎁 Заработано на друзьях: <b>{fmt_stars(fresh.ref_earned)} ⭐</b>\n\n"
        f"💰 Баланс: <b>{fmt_stars(fresh.balance)} ⭐️</b>\n\n"
        "⚠️ <i>Награда начисляется только после успешной проверки выполненных заданий.</i>",
        kb.back(),
    )


# ───────────── профиль ─────────────
@router.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery, user) -> None:
    fresh = await services.get_user(user.id)
    total, _ = await services.referral_counts(user.id)
    nick = _bold(fresh, f"@{esc(fresh.username)}") if fresh.username else "—"
    amounts = settings.withdraw_amounts()
    goal = min(amounts) * 100 if amounts else 0
    await cb.answer()
    await show(
        cb,
        title("🪪", "Профиль")
        + f"👤 Ник: {nick}\n"
        f"🆔 ID: <code>{fresh.id}</code>\n\n"
        f"👥 Друзей приглашено: <b>{total}</b>\n"
        f"✅ Заданий выполнено: <b>{fresh.tasks_done}</b>\n"
        f"🏆 Заработано: <b>{fmt_stars(fresh.earned)} ⭐️</b>\n"
        f"💰 Баланс: <b>{fmt_stars(fresh.balance)} ⭐️</b>\n\n"
        + (f"🎯 До вывода {min(amounts)} ⭐:\n<code>{bar(fresh.balance, goal)}</code>" if goal else ""),
        kb.back(),
    )


# ───────────── вывод ─────────────
_task_locks: dict[int, asyncio.Lock] = {}  # один человек — одна проверка задания за раз
_wd_locks: dict[int, asyncio.Lock] = {}  # защита от двойного нажатия: две заявки на один и тот же баланс

STATUS_LABEL = {"pending": "⏳ ожидает", "done": "✅ выполнена", "rejected": "❌ отклонена"}


@router.callback_query(F.data == "withdraw")
async def cb_withdraw(cb: CallbackQuery, user) -> None:
    fresh = await services.get_user(user.id)
    reserved = await services.reserved_for_withdrawals(user.id)
    amounts = ", ".join(str(a) for a in settings.withdraw_amounts())
    text = (
        title("💫", "Вывод звёзд")
        + f"💰 Баланс: <b>{fmt_stars(fresh.balance)} ⭐</b>\n"
    )
    if reserved:
        text += f"⏳ В заявках: <b>{fmt_stars(reserved)} ⭐</b>\n✅ Доступно: <b>{fmt_stars(fresh.balance - reserved)} ⭐</b>\n"
    text += (
        f"\n🔢 Суммы: <b>{amounts}</b>\n"
        f"⏳ Рассмотрение заявки: <b>{esc(settings.withdraw_term())}</b>\n\n"
        "👇 Выбери сумму — будет создана заявка:"
    )
    await cb.answer()
    await show(cb, text, kb.withdraw_menu())


@router.callback_query(F.data.startswith("wd:"))
async def cb_withdraw_amount(cb: CallbackQuery, user, bot: Bot) -> None:
    amount = int(cb.data.split(":")[1])
    if amount not in settings.withdraw_amounts():
        await cb.answer("Эта сумма больше недоступна", show_alert=True)
        return

    if await fraud.score(user.id) >= config.FRAUD_HOLD:
        await moderation.evaluate(bot, user.id)
        await cb.answer("⏳ Вывод временно недоступен: аккаунт на проверке у администратора.", show_alert=True)
        return

    async with _wd_locks.setdefault(user.id, asyncio.Lock()):
        w = await services.create_withdrawal(user.id, amount)
        if w is None:
            fresh = await services.get_user(user.id)
            free = fresh.balance - await services.reserved_for_withdrawals(user.id)
            await cb.answer(
                f"Недостаточно звёзд: нужно {amount} ⭐, доступно {fmt_stars(max(free, 0))} ⭐",
                show_alert=True,
            )
            return

    fresh = await services.get_user(user.id)
    delivered = await notify.notify_admins_new(bot, w, fresh)
    if not delivered:
        log.error("заявка №%s создана, но ни один админ не получил уведомление", w.id)
    await cb.answer("✅ Заявка создана")
    await show(
        cb,
        title("✅", f"Заявка №{w.id} создана")
        + f"💎 Сумма: <b>{amount} ⭐</b>\n\n"
        f"⏳ Ожидайте принятия заявки администратором (<b>{esc(settings.withdraw_term())}</b>).\n\n"
        "<i>Звёзды спишутся с баланса в боте, когда заявка будет закрыта. "
        "Мы пришлём уведомление.</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [kb.B("📋 Мои заявки", "my_wd", style=kb.P)],
                [kb.B("⬅️ Назад", "withdraw")],
            ]
        ),
    )


@router.callback_query(F.data == "my_wd")
async def cb_my_withdrawals(cb: CallbackQuery, user) -> None:
    items = await services.user_withdrawals(user.id)
    text = title("📋", "Мои заявки")
    if not items:
        text += "Заявок пока нет."
    for w in items:
        text += f"№{w.id} · <b>{fmt_short(w.amount)} ⭐</b> · {STATUS_LABEL[w.status]}\n"
    await cb.answer()
    await show(cb, text, kb.back("withdraw"))


# ───────────── статистика ─────────────
@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery) -> None:
    st = await services.public_stats()
    await cb.answer()
    await show(
        cb,
        title("📊", "Статистика")
        + f"👥 Всего пользователей: <b>{st['users']}</b>\n"
        f"💸 Всего выплачено: <b>{fmt_stars(st['paid'])} ⭐</b>\n\n"
        "👇 Рейтинги:",
        kb.stats_menu(),
    )


TOPS = {
    "balance": ("💰", "Топ по балансу", "⭐"),
    "refs": ("👥", "Топ по рефералам", "друзей"),
    "earned": ("🏆", "Топ по заработку", "⭐"),
}


@router.callback_query(F.data.startswith("top:"))
async def cb_top(cb: CallbackQuery) -> None:
    field = cb.data.split(":")[1]
    if field not in TOPS:
        await cb.answer()
        return
    emoji, name, unit = TOPS[field]
    rows = await services.top_by(field)
    text = title(emoji, name)
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        text += "Пока пусто — стань первым! 🚀"
    for i, (u, value) in enumerate(rows):
        shown = value if field == "refs" else fmt_stars(value)
        text += f"{medals[i] if i < 3 else f'<b>{i + 1}.</b>'} {public_name(u)} — <b>{shown}</b> {unit}\n"
    await cb.answer()
    await show(cb, text, kb.back("stats"))
