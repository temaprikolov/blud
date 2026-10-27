"""Спонсорские подписки (ОП) от рекламных сетей Traffy, PiarFlow и Axionna.

Стена спонсоров на входе — только Traffy и PiarFlow. Axionna даёт задания ТОЛЬКО в «Заработать звёзды».

Работает через их API: получаем список спонсоров для пользователя, показываем кнопками-ссылками и кнопку
«Проверить»; пускаем дальше, когда всё выполнено. Любая ошибка сети/API = пускаем человека (бот не встаёт).
Каждая сеть включается своим ключом в .env (пусто = выключена).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

import config

log = logging.getLogger(__name__)

PASS_TTL = 3600  # после успешного прохождения не дёргаем сети час
_passed: dict[int, float] = {}
_pending: dict[int, list["Task"]] = {}  # что показали пользователю — это же и проверяем
_pf_key: Optional[str] = None


@dataclass
class Task:
    provider: str  # traffy / piarflow / axionna
    key: str  # assignment_id (Traffy), ссылка (PiarFlow) или id задания (Axionna)
    title: str
    link: str
    button: str = "Подписаться"
    ident: str = ""  # стабильный адрес задания для учёта в БД (у Axionna ссылка может быть персональной)

    @property
    def uid(self) -> str:
        return self.ident or self.link


def traffy_enabled() -> bool:
    return bool(config.TRAFFY_API_KEY)


def piarflow_enabled() -> bool:
    return bool(config.PIARFLOW_API_TOKEN or config.PIARFLOW_BOT_KEY)


def axionna_enabled() -> bool:
    return bool(config.AXIONNA_API_KEY)


def enabled() -> bool:
    """Есть ли хоть одна сеть (для заданий)."""
    return traffy_enabled() or piarflow_enabled() or axionna_enabled()


def wall_enabled() -> bool:
    """Стена спонсоров на входе: Axionna в ней не участвует."""
    return traffy_enabled() or piarflow_enabled()


async def _post(url: str, key_header: dict, body: dict, method: str = "POST") -> tuple[int, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
        async with s.request(method, url, json=body if method == "POST" else None, headers=key_header,
                             allow_redirects=False) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"text": (await r.text())[:300]}
            return r.status, data


# ───────────── Traffy ─────────────
def _traffy_base() -> str:
    return config.TRAFFY_URL.split("/publisher")[0].rstrip("/") + "/publisher"


def _traffy_headers() -> dict:
    return {"x-publisher-api-key": config.TRAFFY_API_KEY, "Content-Type": "application/json"}


async def traffy_fetch(u, limit: int = 5) -> list[Task]:
    body = {"telegram_id": u.id, "limit": limit, "language_code": getattr(u, "language_code", None) or "ru"}
    if getattr(u, "first_name", None):
        body["first_name"] = u.first_name
    if getattr(u, "username", None):
        body["username"] = u.username
    st, data = await _post(_traffy_base() + "/tasks", _traffy_headers(), body)
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"Traffy /tasks HTTP {st}: {str(data)[:200]}")
    out = []
    for t in data.get("tasks") or []:
        if t.get("target_link") and t.get("assignment_id"):
            out.append(Task("traffy", str(t["assignment_id"]), t.get("title") or "Спонсор", t["target_link"],
                            t.get("button_text") or "Подписаться"))
    return out


async def traffy_pending(u, tasks: list[Task], strict: bool = False) -> list[Task]:
    st, data = await _post(_traffy_base() + "/tasks/check", _traffy_headers(),
                           {"telegram_id": u.id, "assignment_ids": [t.key for t in tasks]})
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"Traffy /tasks/check HTTP {st}: {str(data)[:200]}")
    ok = ("completed",) if strict else ("completed", "rejected")  # strict (награда): только честно выполненное
    done = {r.get("assignment_id") for r in data.get("results") or []
            if r.get("status") in ok or (not strict and r.get("reason") == "already_completed")}
    return [t for t in tasks if t.key not in done]


# ───────────── PiarFlow ─────────────
_PF = "https://piarflow.com/v1"


async def _pf_bot_key(bot_token: str) -> Optional[str]:
    """Ключ бота: из .env, иначе получаем по токену аккаунта (находим бота или регистрируем)."""
    global _pf_key
    if config.PIARFLOW_BOT_KEY:
        return config.PIARFLOW_BOT_KEY
    if _pf_key:
        return _pf_key
    h = {"Authorization": f"Bearer {config.PIARFLOW_API_TOKEN}", "Content-Type": "application/json"}
    bot_id = int(bot_token.split(":")[0])
    st, data = await _post(f"{_PF}/factory/bots/{bot_id}", h, {}, method="GET")
    if st == 404:
        st, data = await _post(f"{_PF}/factory/bots", h, {"bot_token": bot_token})
    if st == 401:  # это не токен аккаунта — скорее всего, друг дал сразу ключ бота
        log.info("PiarFlow: токен не подошёл как токен аккаунта, пробую как ключ бота")
        _pf_key = config.PIARFLOW_API_TOKEN
        return _pf_key
    if st >= 400 or not isinstance(data, dict) or not (data.get("bot") or {}).get("api_key"):
        raise RuntimeError(f"PiarFlow: не получил ключ бота, HTTP {st}: {str(data)[:200]}")
    _pf_key = data["bot"]["api_key"]
    log.info("PiarFlow: ключ бота получен")
    return _pf_key


async def pf_fetch(u, limit: int = 5) -> list[Task]:
    key = await _pf_bot_key(config.BOT_TOKEN)
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    st, data = await _post(f"{_PF}/sponsors", h, {"user_id": u.id, "chat_id": u.id, "max_sponsors": limit})
    if st in (403, 404):  # пользователь не может выполнить / заданий нет
        return []
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"PiarFlow /sponsors HTTP {st}: {str(data)[:200]}")
    out = []
    for s in data.get("sponsors") or []:
        if s.get("link") and s.get("status") == "unsubscribed":
            name = s["link"].split("?")[0].rstrip("/").split("/")[-1]
            if name.startswith("+"):
                title = "Канал-спонсор"
            elif name.lower().endswith("bot") or "startapp=" in s["link"] or "start=" in s["link"]:
                title = f"Бот @{name}"
            else:
                title = f"@{name}"
            btn = "Запустить" if title.startswith("Бот") else "Подписаться"
            out.append(Task("piarflow", s["link"], title, s["link"], btn))
    return out


async def pf_pending(u, tasks: list[Task], strict: bool = False) -> list[Task]:
    key = await _pf_bot_key(config.BOT_TOKEN)
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    st, data = await _post(f"{_PF}/sponsors/check", h, {"user_id": u.id, "links": [t.key for t in tasks]})
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"PiarFlow /sponsors/check HTTP {st}: {str(data)[:200]}")
    got = {s.get("link"): s.get("status") for s in data.get("sponsors") or []}
    if strict:  # награда человеку — только за засчитанную подписку
        return [t for t in tasks if got.get(t.key) != "subscribed"]
    return [t for t in tasks if got.get(t.key) == "unsubscribed"]


# ───────────── Axionna ─────────────
def _ax_headers() -> dict:
    return {"Authorization": f"Bearer {config.AXIONNA_API_KEY}", "Content-Type": "application/json"}


def _ax_title(sp: dict) -> tuple[str, str]:
    """(название, текст кнопки) для задания Axionna."""
    link = str(sp.get("target_url") or sp.get("link") or "")
    kind = str(sp.get("target_kind") or "").lower()
    name = link.split("?")[0].rstrip("/").split("/")[-1]
    is_bot = kind == "bot" or name.lower().endswith("bot") or "start=" in link or "startapp=" in link
    raw = str(sp.get("title") or "").strip()
    if raw:
        title = raw
    elif name.startswith("+") or not name:
        title = "Канал-спонсор"
    else:
        title = f"Бот @{name}" if is_bot else f"@{name}"
    if is_bot and not title.startswith("Бот"):
        title = f"Бот: {title}"
    btn = str(sp.get("button_text") or "").strip() or ("Запустить" if is_bot else "Подписаться")
    return title, btn


async def ax_fetch(u, limit: int = 5) -> list[Task]:
    body: dict = {"user_id": u.id, "chat_id": u.id, "max_sponsors": max(1, min(int(limit), 20))}
    if getattr(u, "username", None):
        body["username"] = u.username
    if getattr(u, "language_code", None):
        body["language_code"] = u.language_code
    st, data = await _post(config.AXIONNA_URL.rstrip("/") + "/sponsors", _ax_headers(), body)
    if st in (403, 404):  # пользователь не подходит / заданий нет
        return []
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"Axionna /sponsors HTTP {st}: {str(data)[:200]}")
    if str(data.get("status") or "ok").lower() not in ("ok", "success"):
        raise RuntimeError(f"Axionna /sponsors: {str(data)[:200]}")
    out: list[Task] = []
    seen: set[str] = set()
    for sp in data.get("sponsors") or []:
        if not isinstance(sp, dict) or not sp.get("id") or not sp.get("link"):
            continue
        if sp.get("status") in ("subscribed", "ok") or sp.get("already_joined"):
            continue
        tid = str(sp["id"])
        if tid in seen:
            continue
        seen.add(tid)
        title, btn = _ax_title(sp)
        out.append(Task("axionna", tid, title, str(sp["link"]), btn, ident=str(sp.get("target_url") or sp["link"])))
    return out


async def _ax_check(u, tasks: list[Task]) -> dict[str, str]:
    st, data = await _post(config.AXIONNA_URL.rstrip("/") + "/check", _ax_headers(),
                           {"user_id": u.id, "task_ids": [t.key for t in tasks]})
    if st >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"Axionna /check HTTP {st}: {str(data)[:200]}")
    return {str(r.get("id")): str(r.get("status")) for r in data.get("results") or [] if isinstance(r, dict)}


async def ax_pending(u, tasks: list[Task], strict: bool = False) -> list[Task]:
    got = await _ax_check(u, tasks)
    if strict:  # награда человеку — только за status == ok (not_allowed награды не даёт никогда)
        return [t for t in tasks if got.get(t.key) != "ok"]
    # стена спонсоров: not_allowed — сеть сняла задание, человека им не держим
    return [t for t in tasks if got.get(t.key) == "not_ok"]


# ───────────── общий слой ─────────────
_fetch_cache: dict[int, tuple[float, list[Task]]] = {}
FETCH_TTL = 25  # заявки сетей на одного человека не дёргаем чаще раза в 25 сек — иначе бот тормозит


async def fetch(u, limit: int = 5, wall: bool = False) -> list[Task]:
    """Задания от сетей. wall=True — для стены спонсоров на входе (без Axionna); иначе — для «Заработать звёзды»."""
    if not wall:
        hit = _fetch_cache.get(u.id)
        if hit and hit[0] > time.time():
            return list(hit[1])
    jobs = [(traffy_enabled(), traffy_fetch), (piarflow_enabled(), pf_fetch)]
    if not wall:
        jobs.append((axionna_enabled(), ax_fetch))

    async def run(fn):
        try:
            return await asyncio.wait_for(fn(u, limit), timeout=6)
        except Exception as e:  # noqa: BLE001
            log.warning("Спонсоры: %s", e)
            return []

    results = await asyncio.gather(*(run(fn) for on, fn in jobs if on))  # сети опрашиваем параллельно
    tasks: list[Task] = [t for r in results for t in r]
    if not wall:
        _fetch_cache[u.id] = (time.time() + FETCH_TTL, list(tasks))
    return tasks


WALL_MAX = 4  # сколько спонсоров показываем на входе — не заваливаем человека кнопками


def _wall_pick(tasks: list[Task]) -> list[Task]:
    """На входе: не больше WALL_MAX, и один бот не повторяется (у сетей это разные ссылки на одного и того же бота)."""
    seen, out = set(), []
    for t in tasks:
        k = (t.provider, t.title) if t.title.startswith("Бот") else (t.provider, t.link)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    # чередуем сети, чтобы не было четырёх подряд от одной
    by = {}
    for t in out:
        by.setdefault(t.provider, []).append(t)
    mixed = []
    while any(by.values()):
        for lst in by.values():
            if lst:
                mixed.append(lst.pop(0))
    return mixed[:WALL_MAX]


async def gate(u, force: bool = False) -> list[Task]:
    """Пустой список — пускаем. Иначе — спонсоры, на которых человеку надо подписаться."""
    if not wall_enabled():
        return []
    if not force and _passed.get(u.id, 0) > time.time():
        return []
    if force and _pending.get(u.id):
        tasks = _pending[u.id]
        left: list[Task] = []
        for prov, fn in (("traffy", traffy_pending), ("piarflow", pf_pending)):
            mine = [t for t in tasks if t.provider == prov]
            if not mine:
                continue
            try:
                left += await fn(u, mine)
            except Exception as e:
                log.warning("Спонсоры: проверка не удалась: %s", e)
        if left:
            fresh = await fetch(u, wall=True)  # обновлённый список (истёкшие заменятся)
            tasks = _wall_pick(fresh) or left
            _pending[u.id] = tasks
            return tasks
    else:
        tasks = _wall_pick(await fetch(u, wall=True))
        if tasks:
            _pending[u.id] = tasks
            return tasks
    _pending.pop(u.id, None)
    _passed[u.id] = time.time() + PASS_TTL
    return []


async def task_state(u, task: Task) -> str:
    """Задание из «Заработать звёзды»: 'done' — выполнено, 'pending' — ещё нет, 'gone' — сеть сняла задание."""
    try:
        if task.provider == "axionna":
            # подписка у сети засчитывается с задержкой — даём пару повторных попыток, а не отказываем сразу
            for i in range(3):
                got = (await _ax_check(u, [task])).get(task.key)
                log.info("Axionna check user=%s task=%s -> %s", u.id, task.key, got)
                if got == "ok":
                    return "done"
                if got == "not_allowed":
                    return "gone"
                if i < 2:
                    await asyncio.sleep(1.2)
            return "pending"
        fn = traffy_pending if task.provider == "traffy" else pf_pending
        return "pending" if await fn(u, [task], strict=True) else "done"
    except Exception as e:  # noqa: BLE001
        log.warning("Спонсоры: проверка задания не удалась: %s", e)
        return "error"


async def is_done(u, task: Task) -> bool:
    return await task_state(u, task) == "done"


def forget(user_id: int) -> None:
    _passed.pop(user_id, None)
    _pending.pop(user_id, None)
    _fetch_cache.pop(user_id, None)
