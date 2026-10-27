import asyncio
import fcntl
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

import audit
import config
import handlers_admin
import handlers_checks
import handlers_user
import middlewares
import services
import settings
import sponsors
from antispam import RateLimitMiddleware
from db import init_db
from middlewares import AccessMiddleware

log = logging.getLogger("main")

# Ошибки, которые не являются сбоем: старая кнопка, повторное нажатие и т.п.
_HARMLESS = ("query is too old", "query ID is invalid", "message is not modified", "message to edit not found",
             "message can't be edited", "message to delete not found")


class FallbackSession(AiohttpSession):
    """Сначала идём через прокси (TELEGRAM_PROXY); если он не отвечает — тот же запрос уходит напрямую.
    После сбоя прокси отключается на 60 секунд, потом пробуется снова."""

    def __init__(self, proxy: str, **kw) -> None:
        super().__init__(proxy=proxy, **kw)
        self._direct = AiohttpSession(**kw)
        self._proxy_down_until = 0.0

    async def make_request(self, bot, method, timeout=None):
        if time.time() >= self._proxy_down_until:
            try:
                return await super().make_request(bot, method, timeout=min(timeout or 15, 15))
            except TelegramNetworkError as e:
                log.warning("Прокси не работает (%s) — иду напрямую, повтор через 60 c", e)
                self._proxy_down_until = time.time() + 60
        return await self._direct.make_request(bot, method, timeout=timeout)

    async def close(self) -> None:
        await super().close()
        await self._direct.close()


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:  # файл лога рядом с ботом: после падения видно, что было последним
        fh = RotatingFileHandler(config.BASE_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # не спамим «Update id=... is handled»


def acquire_lock():
    """Один экземпляр на папку: второй запуск того же бота сразу скажет об этом и выйдет."""
    f = open(config.BASE_DIR / ".bot.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("❌ Этот бот уже запущен (другой screen/процесс). Останови старый: ps aux | grep main.py", file=sys.stderr)
        raise SystemExit(3)
    f.write(str(os.getpid()))
    f.flush()
    return f


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        exc = event.exception
        if isinstance(exc, TelegramBadRequest) and any(x in str(exc) for x in _HARMLESS):
            return True
        uid = audit.uid_of(event.update)
        if isinstance(exc, TelegramNetworkError):
            log.warning("Сеть: %s", exc)
            audit.event(uid, "NET", f"{type(exc).__name__}: {str(exc)[:100]}")
            return True
        audit.event(uid, "ERR", f"{type(exc).__name__}: {str(exc)[:120]}")
        log.error("Ошибка в обработчике: %r", exc, exc_info=exc)
        return True  # ошибка одного пользователя не должна ронять бота

    # журнал действий — самым первым, чтобы видеть и то, что потом отрежет анти-спам
    journal = audit.AuditMiddleware()
    dp.message.outer_middleware(journal)
    dp.callback_query.outer_middleware(journal)

    # порядок важен: потом анти-спам (режет флуд до БД и проверок подписки), потом доступ
    limiter = RateLimitMiddleware()
    dp.message.outer_middleware(limiter)
    dp.callback_query.outer_middleware(limiter)

    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)

    dp.include_router(audit.router)  # /log для админов
    dp.include_router(handlers_admin.router)  # раньше пользовательского: у админа свои состояния (FSM)
    dp.include_router(handlers_checks.router)
    dp.include_router(handlers_user.router)
    return dp


def make_bot() -> Bot:
    # TELEGRAM_PROXY в .env (http://user:pass@host:port или socks5://...) — по желанию; не отвечает = идём напрямую
    proxy = (os.getenv("TELEGRAM_PROXY") or "").strip()
    session = FallbackSession(proxy, timeout=60) if proxy else AiohttpSession(timeout=60)
    return Bot(config.BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def housekeeping() -> None:
    """Раз в 10 минут чистим просроченные записи в кэшах — иначе память растёт неделями."""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        try:
            for d in (middlewares._ok_cache, middlewares._banned_told, sponsors._passed):
                for k in [k for k, v in d.items() if v < now - 300]:
                    d.pop(k, None)
            for k in [k for k, (t, _) in sponsors._fetch_cache.items() if t < now]:
                sponsors._fetch_cache.pop(k, None)
            if len(sponsors._pending) > 5000:
                sponsors._pending.clear()
            if len(handlers_user._shown) > 20000:
                handlers_user._shown.clear()
        except Exception as e:  # noqa: BLE001
            log.warning("housekeeping: %s", e)


async def connect(bot: Bot) -> None:
    """Ждём связи с Telegram сколько потребуется — на нестабильной сети бот не падает, а ждёт."""
    delay = 2
    while True:
        try:
            me = await bot.me()  # кэшируется: start_polling не повторит этот запрос
            log.info("Запущен @%s", me.username)
            audit.event(0, "SYS", f"бот @{me.username} запущен")
            await bot.delete_webhook(drop_pending_updates=True)  # старые нажатия кнопок уже не ответить
            return
        except (TelegramNetworkError, asyncio.TimeoutError, OSError) as e:
            log.warning("Нет связи с Telegram (%s). Повтор через %s c", e, delay)
            audit.event(0, "NET", f"нет связи с Telegram: {type(e).__name__}, повтор через {delay}c")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def main() -> None:
    setup_logging()
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (см. .env)")
    lock = acquire_lock()  # noqa: F841 — держим файл открытым, пока работает процесс

    await init_db()
    await settings.load()
    await services.seed_default_channel()

    dp = build_dispatcher()
    hk = asyncio.create_task(housekeeping())
    fails, delay = 0, 3
    try:
        while True:  # что бы ни случилось (сеть, редкая ошибка) — поднимаем polling заново, процесс не завершаем
            bot = make_bot()
            started = time.time()
            try:
                await connect(bot)
                await dp.start_polling(bot, allowed_updates=["message", "callback_query"],
                                       polling_timeout=30, handle_signals=True)
                break  # штатная остановка (Ctrl+C / SIGTERM)
            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            except Exception as e:  # noqa: BLE001
                fails = 0 if time.time() - started > 120 else fails + 1  # долго проработал — сбой «разовый»
                delay = min(3 * (2 ** fails), 60)
                audit.event(0, "SYS", f"polling остановился: {type(e).__name__}: {str(e)[:100]}")
                log.error("Polling остановился: %r. Перезапуск через %s c", e, delay, exc_info=e)
                await asyncio.sleep(delay)
            finally:
                await bot.session.close()
    finally:
        hk.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
