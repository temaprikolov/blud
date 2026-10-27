"""Админка: чеки (создание, пост для канала, публикация, управление)."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import services
from db import utcnow
from handlers_admin import IsAdmin, resolve_chat
from handlers_user import show
from utils import esc, fmt_dt, fmt_short, mention, parse_stars

log = logging.getLogger(__name__)
router = Router(name="checks")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

LINE = "━━━━━━━━━━━━━━━━━━"
MAX_PEOPLE = 100000
MAX_CHANNELS = 5


class C(StatesGroup):
    total = State()
    people = State()
    per = State()
    duration = State()
    password = State()
    channel = State()
    channel_url = State()
    publish = State()


# ───────────── утилиты ─────────────
async def say(event: Message | CallbackQuery, text: str, markup=None) -> None:
    """Отвечает на сообщение или правит сообщение с кнопкой — в зависимости от источника."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        await show(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


def parse_duration(text: str) -> int | None:
    """'30м', '12ч', '2д' (также m/h/d) -> секунды. От 1 минуты до 365 дней."""
    m = re.fullmatch(r"(\d{1,6})\s*(м|мин|m|min|ч|час|h|д|дн|d)", (text or "").strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    seconds = n * (60 if unit in ("м", "мин", "m", "min") else 3600 if unit in ("ч", "час", "h") else 86400)
    return seconds if 60 <= seconds <= 365 * 86400 else None


def fmt_duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} дн."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч."
    return f"{max(seconds // 60, 1)} мин."


def check_link(chk) -> str:
    return f"https://t.me/{config.BOT_USERNAME}?start=chk_{chk.code}"


def post_text(chk) -> str:
    """Текст публикации для канала (пароль в нём не раскрывается)."""
    if chk.max_people is not None:
        text = (
            f"🧾 <b>Чек на {fmt_short(chk.per_person * chk.max_people)} ⭐</b>\n{LINE}\n\n"
            f"🎁 Каждый получит: <b>{fmt_short(chk.per_person)} ⭐</b>\n"
            f"👥 Мест: <b>{chk.max_people}</b>\n"
        )
    else:
        text = f"🧾 <b>Чек на {fmt_short(chk.per_person)} ⭐</b>\n{LINE}\n\n🎁 Каждый получит: <b>{fmt_short(chk.per_person)} ⭐</b>\n"
    if chk.expires_at:
        text += f"⏱ Действует до: <b>{fmt_dt(chk.expires_at)}</b>\n"
    if chk.password:
        text += "🔐 Нужен пароль\n"
    channels = services.check_channels(chk)
    if channels:
        text += "📢 Нужна подписка: " + ", ".join(esc(c["title"]) for c in channels) + "\n"
    return text + "\nНажми кнопку ниже, чтобы активировать 👇"


STATUS = {None: "🟢 активен", "inactive": "⚪️ отключён", "expired": "⌛ истёк по времени", "exhausted": "✅ все места заняты"}


def card_text(chk) -> str:
    channels = services.check_channels(chk)
    limit = f"{chk.activations} / {chk.max_people}" if chk.max_people is not None else f"{chk.activations} (без лимита)"
    text = (
        f"🧾 <b>Чек №{chk.id}</b>\n{LINE}\n\n"
        f"💎 Каждый получает: <b>{fmt_short(chk.per_person)} ⭐</b>\n"
        f"👥 Активаций: <b>{limit}</b>\n"
    )
    if chk.max_people is not None:
        text += f"💰 Общая сумма: <b>{fmt_short(chk.per_person * chk.max_people)} ⭐</b>\n"
    text += f"💸 Выдано: <b>{fmt_short(chk.per_person * chk.activations)} ⭐</b>\n"
    text += f"⏱ Действует до: {fmt_dt(chk.expires_at)}\n" if chk.expires_at else "⏱ По времени не ограничен\n"
    text += f"🔐 Пароль: <code>{esc(chk.password)}</code>\n" if chk.password else "🔓 Пароля нет\n"
    text += (
        "📢 Подписка: " + ", ".join(esc(c["title"]) for c in channels) + "\n" if channels else "📢 Подписка не требуется\n"
    )
    text += f"Статус: {STATUS[services.check_problem(chk)]}\n\n🔗 <code>{check_link(chk)}</code>"
    return text


async def show_card(event: Message | CallbackQuery, chk) -> None:
    await say(event, card_text(chk), kb.check_detail_kb(chk))


# ───────────── список ─────────────
@router.callback_query(F.data == "a:checks")
async def cb_checks(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    checks = await services.list_checks()
    await cb.answer()
    await show(
        cb,
        f"🧾 <b>Чеки</b>\n{LINE}\n\nЗвёзды по чеку зачисляются на баланс в боте. "
        "Чек активируют по ссылке или кнопке из поста в канале.\n\n🟢 — действует, ⚪️ — закончился или отключён.",
        kb.checks_list_kb(checks),
    )


@router.callback_query(F.data.regexp(r"^a:chk:\d+$"))
async def cb_check_view(cb: CallbackQuery) -> None:
    chk = await services.get_check(int(cb.data.split(":")[2]))
    if not chk:
        await cb.answer("Чек не найден", show_alert=True)
        return
    await show_card(cb, chk)


@router.callback_query(F.data.startswith("a:chk_toggle:"))
async def cb_check_toggle(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    await services.toggle_check(cid)
    chk = await services.get_check(cid)
    await show_card(cb, chk)


@router.callback_query(F.data.startswith("a:chk_acts:"))
async def cb_check_acts(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    chk = await services.get_check(cid)
    if not chk:
        await cb.answer("Чек не найден", show_alert=True)
        return
    rows = await services.check_activation_list(cid)
    text = f"👥 <b>Активации чека №{cid}</b> ({chk.activations})\n{LINE}\n\n"
    if not rows:
        text += "Пока никто не активировал."
    for act, user in rows:
        text += f"{mention(user)} · <code>{user.id}</code> · {fmt_dt(act.created_at)}\n"
    await cb.answer()
    await show(cb, text, kb.back(f"a:chk:{cid}"))


# ───────────── пост и публикация ─────────────
@router.callback_query(F.data.startswith("a:chk_post:"))
async def cb_check_post(cb: CallbackQuery) -> None:
    chk = await services.get_check(int(cb.data.split(":")[2]))
    if not chk:
        await cb.answer("Чек не найден", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(post_text(chk), reply_markup=kb.check_post_kb(check_link(chk)))
    await cb.message.answer("☝️ Это готовый пост — перешли его в канал (кнопка сохранится) или нажми «📣 В канал».")


@router.callback_query(F.data.startswith("a:chk_pub:"))
async def cb_check_publish(cb: CallbackQuery, state: FSMContext) -> None:
    cid = int(cb.data.split(":")[2])
    chk = await services.get_check(cid)
    if not chk:
        await cb.answer("Чек не найден", show_alert=True)
        return
    await state.set_state(C.publish)
    await state.update_data(check_id=cid)
    await say(
        cb,
        "📣 <b>Опубликовать чек в канале</b>\n\nВыбери канал из списка обязательных подписок или отправь "
        "@username / ID другого канала. Бот должен быть там администратором с правом публиковать.",
        kb.check_publish_kb(cid, await services.list_channels()),
    )


async def publish(bot: Bot, chk, chat_id: int) -> str | None:
    """None — успех, иначе текст ошибки."""
    try:
        await bot.send_message(chat_id, post_text(chk), reply_markup=kb.check_post_kb(check_link(chk)))
    except TelegramAPIError as e:
        return str(e)
    return None


@router.callback_query(F.data.startswith("a:chk_pubto:"))
async def cb_check_publish_to(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, _, cid, chat_id = cb.data.split(":")
    chk = await services.get_check(int(cid))
    if not chk:
        await cb.answer("Чек не найден", show_alert=True)
        return
    err = await publish(bot, chk, int(chat_id))
    await state.clear()
    if err:
        await cb.answer(f"Не удалось: {err}"[:190], show_alert=True)
        return
    await cb.answer("✅ Опубликовано", show_alert=False)
    await show_card(cb, chk)


@router.message(C.publish)
async def msg_check_publish(message: Message, state: FSMContext, bot: Bot) -> None:
    cid = (await state.get_data())["check_id"]
    chk = await services.get_check(cid)
    chat, err = await resolve_chat(bot, message.text or "")
    if err or not chk:
        await message.answer(err or "Чек не найден", reply_markup=kb.cancel_kb())
        return
    err = await publish(bot, chk, chat.id)
    await state.clear()
    await message.answer(f"⚠️ Не удалось опубликовать: {esc(err)}" if err else "✅ Опубликовано в канале.")
    await show_card(message, chk)


# ───────────── создание ─────────────
@router.callback_query(F.data == "a:chk_new")
async def cb_check_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await say(
        cb,
        f"🧾 <b>Новый чек</b>\n{LINE}\n\nЧем ограничить чек?\n\n"
        "👥 <b>По количеству</b> — задаёшь сумму и число человек, чек закрывается, когда все места заняты.\n"
        "⏱ <b>По времени</b> — каждый получает фиксированную сумму, пока чек действует.",
        kb.check_type_kb(),
    )


@router.callback_query(F.data == "a:chk_type:count")
async def cb_type_count(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(mode="count")
    await state.set_state(C.total)
    await say(cb, "💰 Сколько <b>всего звёзд</b> в чеке? Например: <code>100</code>", kb.cancel_kb())


@router.message(C.total)
async def msg_total(message: Message, state: FSMContext) -> None:
    cents = parse_stars(message.text or "")
    if cents is None or cents > 10_000_000:
        await message.answer("Введи положительное число звёзд, например <code>100</code>", reply_markup=kb.cancel_kb())
        return
    await state.update_data(total=cents)
    await state.set_state(C.people)
    await message.answer("👥 На <b>сколько человек</b> рассчитан чек? Например: <code>5</code>", reply_markup=kb.cancel_kb())


@router.message(C.people)
async def msg_people(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= MAX_PEOPLE:
        await message.answer(f"Введи целое число от 1 до {MAX_PEOPLE}.", reply_markup=kb.cancel_kb())
        return
    people = int(text)
    total = (await state.get_data())["total"]
    if total < people:
        await message.answer(
            f"На {people} человек нужно минимум {fmt_short(people)} ⭐ (по 0.01 каждому). Введи другое число.",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(people=people)
    await state.set_state(C.per)
    equal = total // people if total % people == 0 else None
    hint = f"Не больше {fmt_short(total // people)} ⭐, чтобы уложиться в {fmt_short(total)} ⭐ на {people} чел."
    await message.answer(
        f"🎁 Сколько звёзд получит <b>один человек</b> за активацию?\n<i>{hint}</i>",
        reply_markup=kb.check_equal_kb(equal) if equal else kb.cancel_kb(),
    )


@router.callback_query(F.data == "a:chk_equal")
async def cb_equal(cb: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "total" not in d or "people" not in d:
        await cb.answer("Начни создание заново", show_alert=True)
        return
    await state.update_data(per=d["total"] // d["people"])
    await ask_password(cb, state)


@router.callback_query(F.data == "a:chk_type:time")
async def cb_type_time(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(mode="time")
    await state.set_state(C.per)
    await say(
        cb,
        "🎁 Сколько звёзд получит <b>один человек</b> за активацию? Например: <code>5</code>\n"
        "<i>Число участников в этом режиме не ограничено — только временем.</i>",
        kb.cancel_kb(),
    )


@router.message(C.per)
async def msg_per(message: Message, state: FSMContext) -> None:
    cents = parse_stars(message.text or "")
    d = await state.get_data()
    if cents is None or cents > 10_000_000:
        await message.answer("Введи положительное число звёзд, например <code>20</code>", reply_markup=kb.cancel_kb())
        return
    if d.get("mode") == "count" and cents * d["people"] > d["total"]:
        await message.answer(
            f"Не хватает бюджета: {fmt_short(cents)} × {d['people']} = {fmt_short(cents * d['people'])} ⭐, "
            f"а в чеке {fmt_short(d['total'])} ⭐. Введи сумму поменьше.",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(per=cents)
    if d.get("mode") == "time":
        await state.set_state(C.duration)
        await message.answer(
            "⏱ На <b>какое время</b> чек? Примеры: <code>30м</code>, <code>12ч</code>, <code>2д</code>",
            reply_markup=kb.cancel_kb(),
        )
    else:
        await ask_password(message, state)


@router.message(C.duration)
async def msg_duration(message: Message, state: FSMContext) -> None:
    seconds = parse_duration(message.text or "")
    if seconds is None:
        await message.answer(
            "Не понял. Напиши число с единицей: <code>30м</code>, <code>12ч</code>, <code>2д</code> (от 1 минуты до 365 дней).",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(ttl=seconds)
    await ask_password(message, state)


async def ask_password(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await say(event, "🔐 Защитить чек <b>паролем</b>? Пароль нужно будет ввести при активации.", kb.check_password_kb())


@router.callback_query(F.data == "a:chk_pw:no")
async def cb_pw_no(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(password=None)
    await ask_channels(cb, state)


@router.callback_query(F.data == "a:chk_pw:yes")
async def cb_pw_yes(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(C.password)
    await say(cb, "🔐 Отправь пароль (до 32 символов, регистр важен):", kb.cancel_kb())


@router.message(C.password)
async def msg_password(message: Message, state: FSMContext) -> None:
    pw = (message.text or "").strip()
    if not pw or len(pw) > 32:
        await message.answer("Пароль — от 1 до 32 символов, текстом.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(password=pw)
    await ask_channels(message, state)


async def ask_channels(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    channels = (await state.get_data()).get("channels", [])
    text = (
        "📢 <b>Подписка на канал</b>\n\nМожно потребовать, чтобы человек был подписан на канал(ы) — бот проверит "
        "и скажет, на какой он не подписан.\n"
    )
    if channels:
        text += "\n<b>Добавлены:</b>\n" + "\n".join(f"• {esc(c['title'])}" for c in channels) + "\n"
    await say(event, text, kb.check_channels_kb(bool(channels), len(channels) < MAX_CHANNELS))


@router.callback_query(F.data == "a:chk_ch:add")
async def cb_channel_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(C.channel)
    await say(
        cb,
        "📢 Отправь @username или ID канала. Бот должен быть там <b>администратором</b> — иначе не сможет проверять подписку.",
        kb.cancel_kb(),
    )


@router.message(C.channel)
async def msg_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    chat, err = await resolve_chat(bot, message.text or "")
    if err:
        await message.answer(err, reply_markup=kb.cancel_kb())
        return
    d = await state.get_data()
    channels = d.get("channels", [])
    if any(c["chat_id"] == chat.id for c in channels):
        await message.answer("Этот канал уже добавлен.")
        await ask_channels(message, state)
        return
    if chat.username:
        channels.append({"chat_id": chat.id, "title": chat.title or chat.username, "url": f"https://t.me/{chat.username}"})
        await state.update_data(channels=channels)
        await ask_channels(message, state)
        return
    await state.update_data(pending_chat={"chat_id": chat.id, "title": chat.title or str(chat.id)})
    await state.set_state(C.channel_url)
    await message.answer("Канал приватный. Отправь ссылку-приглашение (https://t.me/+…):", reply_markup=kb.cancel_kb())


@router.message(C.channel_url)
async def msg_channel_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not re.fullmatch(r"https://t\.me/\S+", url):
        await message.answer("Нужна ссылка вида https://t.me/…", reply_markup=kb.cancel_kb())
        return
    d = await state.get_data()
    channels = d.get("channels", [])
    channels.append({**d["pending_chat"], "url": url})
    await state.update_data(channels=channels, pending_chat=None)
    await ask_channels(message, state)


@router.callback_query(F.data == "a:chk_ch:done")
async def cb_finish(cb: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "per" not in d:
        await cb.answer("Начни создание заново", show_alert=True)
        return
    expires = utcnow() + timedelta(seconds=d["ttl"]) if d.get("ttl") else None
    chk = await services.create_check(
        per_person=d["per"],
        max_people=d.get("people") if d.get("mode") == "count" else None,
        expires_at=expires,
        password=d.get("password"),
        channels=d.get("channels", []),
        admin_id=cb.from_user.id,
    )
    await state.clear()
    await cb.answer("✅ Чек создан")
    await show_card(cb, chk)
    await cb.message.answer(post_text(chk), reply_markup=kb.check_post_kb(check_link(chk)))
    await cb.message.answer(
        "☝️ Готовый пост для канала: перешли его туда (кнопка «Активировать чек» сохранится) "
        "или нажми «📣 В канал» в карточке чека."
    )
