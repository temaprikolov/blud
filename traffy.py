"""Интеграция с Traffy (ОП-блок): перед доступом к боту проверяем спонсорские подписки.

POST https://api.traffy.ai/publisher, заголовок x-publisher-api-key, в теле tg_id пользователя.
Если юзер подписан — пускаем. Если нет — Traffy возвращает рекламный пост и ссылки на спонсоров:
показываем их с кнопкой «Проверить подписку». Любая ошибка сети/ответа = пускаем юзера (бот не должен
вставать из-за рекламной сети), причина пишется в лог.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

import config

log = logging.getLogger(__name__)

PASS_TTL = 3600  # после успешной проверки не дёргаем Traffy час
_passed: dict[int, float] = {}

_POS = {"ok", "success", "subscribed", "passed", "allowed", "approved", "done", "true"}
_FLAG_KEYS = ("subscribed", "is_subscribed", "passed", "success", "ok", "allowed", "approved", "completed")
_TEXT_KEYS = ("text", "message", "post", "ad", "caption", "description", "title", "html")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class Result:
    passed: bool
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (название, ссылка)
    raw: Any = None
    error: Optional[str] = None


def enabled() -> bool:
    return bool(config.TRAFFY_API_KEY)


def _walk(o: Any, links: list, texts: list, flags: list, key: str = "") -> None:
    if isinstance(o, dict):
        title = next((str(o[k]) for k in ("title", "name", "channel", "label", "button") if isinstance(o.get(k), str)), "")
        for k in ("url", "link", "invite_link", "href"):
            v = o.get(k)
            if isinstance(v, str) and v.startswith("http"):
                links.append((title, v))
        for k, v in o.items():
            lk = str(k).lower()
            if isinstance(v, bool) and lk in _FLAG_KEYS:
                flags.append(v)
            elif isinstance(v, str):
                if lk == "status":
                    flags.append(v.lower() in _POS)
                elif lk in _TEXT_KEYS and not v.startswith("http"):
                    texts.append(v)
                elif v.startswith("http") and lk not in ("url", "link", "invite_link", "href"):
                    links.append((title, v))
            else:
                _walk(v, links, texts, flags, lk)
    elif isinstance(o, list):
        for i in o:
            if isinstance(i, str) and i.startswith("http"):
                links.append(("", i))
            else:
                _walk(i, links, texts, flags, key)


def parse(data: Any) -> Result:
    links: list[tuple[str, str]] = []
    texts: list[str] = []
    flags: list[bool] = []
    _walk(data, links, texts, flags)
    text = texts[0] if texts else ""
    for u in _URL_RE.findall(text):  # ссылки, вшитые прямо в текст поста
        if u not in [l[1] for l in links]:
            links.append(("", u))
    seen, uniq = set(), []
    for t, u in links:
        if u not in seen:
            seen.add(u)
            uniq.append((t, u))
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        passed = len(data["tasks"]) == 0  # Traffy вернул задания (спонсоров) — значит, надо подписаться
    elif flags:
        passed = all(flags) if any(flags) else False
        if passed is False and not uniq and not text:
            passed = True  # «не пройдено», но показывать нечего — не держим человека
    else:
        passed = not uniq  # без явного флага: нет спонсорских ссылок = пускаем
    return Result(passed=passed, text=text, links=uniq[:8], raw=data)


async def check(user_id: int, use_cache: bool = True) -> Result:
    if not enabled():
        return Result(True)
    if use_cache and _passed.get(user_id, 0) > time.time():
        return Result(True)
    url = config.TRAFFY_URL
    headers = {"x-publisher-api-key": config.TRAFFY_API_KEY, "Content-Type": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            # редиректы идём вручную: aiohttp на 301/302 превращает POST в GET
            for _ in range(4):
                async with s.post(url, json={"tg_id": user_id}, headers=headers, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        url = str(r.url.join(aiohttp.client.URL(r.headers["Location"])))
                        log.info("Traffy redirect -> %s", url)
                        continue
                    body = await r.text()
                    if r.status >= 400:
                        log.warning("Traffy HTTP %s (%s): %s", r.status, url, body[:300])
                        return Result(True, raw=body, error=f"HTTP {r.status} {url}: {body[:300]}")
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        data = {"text": body}
                    break
            else:
                return Result(True, error="слишком много редиректов")
    except Exception as e:  # сеть/таймаут — не блокируем людей
        log.warning("Traffy недоступен: %s", e)
        return Result(True, error=str(e))
    res = parse(data)
    log.info("Traffy user=%s passed=%s raw=%s", user_id, res.passed, str(data)[:300])
    if res.passed:
        _passed[user_id] = time.time() + PASS_TTL
    return res


def forget(user_id: int) -> None:
    _passed.pop(user_id, None)


DOC_PATHS = ["/", "/docs", "/docs-json", "/api", "/api-json", "/swagger", "/swagger-json", "/openapi.json", "/api/docs", "/publisher/docs"]
POST_PATHS = [
    "/publisher/check", "/publisher/subscription", "/publisher/subscriptions", "/publisher/op", "/publisher/verify",
    "/publisher/subscribe", "/publisher/check-subscription", "/publisher/ad", "/publisher/ads", "/publisher/sponsors",
    "/publisher/start", "/publisher/request", "/publisher/user", "/publisher/users", "/publisher/get", "/publisher/send",
    "/publisher/post", "/publisher/offer", "/publisher/offers", "/publisher/task", "/publisher/tasks", "/publisher/channels",
    "/publisher/op-block", "/publisher/gate", "/publisher/access", "/publisher/status", "/publisher/is-subscribed",
    "/v1/publisher", "/api/publisher", "/api/v1/publisher", "/publisher/v1", "/publisher/api",
]


async def probe(tg_id: int) -> list[str]:
    """Диагностика: ищем документацию и живые пути (всё, что не 404, — кандидат)."""
    base = config.TRAFFY_URL.split("/publisher")[0].rstrip("/")
    h = {"x-publisher-api-key": config.TRAFFY_API_KEY, "Content-Type": "application/json"}
    out: list[str] = []
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for path in DOC_PATHS:
            try:
                async with s.get(base + path, headers=h, allow_redirects=False) as r:
                    body = await r.text()
                    if r.status != 404:
                        extra = ""
                        try:
                            import json as _j
                            j = _j.loads(body)
                            if isinstance(j, dict) and "paths" in j:
                                extra = " PATHS: " + ", ".join(f"{m.upper()} {k}" for k, v in j["paths"].items() for m in v)[:1500]
                        except Exception:
                            pass
                        out.append(f"GET {path} → {r.status} {body[:120]!r}{extra}")
            except Exception as e:
                out.append(f"GET {path} → ошибка {e}")
                break
        for path in POST_PATHS:
            try:
                async with s.post(base + path, json={"tg_id": tg_id}, headers=h, allow_redirects=False) as r:
                    body = await r.text()
                    if r.status != 404:
                        out.append(f"POST {path} → {r.status} {body[:200]!r}")
            except Exception as e:
                out.append(f"POST {path} → ошибка {e}")
                break
    return out or ["Все проверенные пути вернули 404"]


async def fetch_docs() -> str:
    """Текст страницы документации Traffy (для диагностики)."""
    import html as _h
    base = config.TRAFFY_URL.split("/publisher")[0].rstrip("/")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(base + "/publisher/docs", headers={"x-publisher-api-key": config.TRAFFY_API_KEY}) as r:
            body = await r.text()
    body = re.sub(r"(?is)<(script|style).*?</\1>", "", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|pre|code|section)>", "\n", body)
    body = _h.unescape(re.sub(r"<[^>]+>", "", body))
    return re.sub(r"\n\s*\n+", "\n\n", body).strip()
