"""Page watches stored in Redis.

A check labels each URL same, new, changed, removed, or error.
When a goal is set, NanoGPT decides whether a text change is real.
Webhook URLs must be public https. Delivery itself is a separate function
so tests can point it at a local catcher after validation has already run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from site_tools import ToolError, ensure_http_url, normalize_snapshot

logger = logging.getLogger("crawl4ai.page_monitor")

INDEX_KEY = "c4ai:page-monitors"
MIN_INTERVAL_SECONDS = 15 * 60


def parse_schedule(text: str) -> int:
    value = (text or "").strip().lower()
    if value in {"hourly", "every hour"}:
        return 3600
    if value in {"daily", "every day"} or value.startswith("daily at "):
        return 86400
    if value.startswith("every ") and value.endswith(" minutes"):
        number = value[len("every "):-len(" minutes")].strip()
        if number.isdigit():
            minutes = int(number)
            if minutes < 15:
                raise ToolError(400, "Schedule must be at least 15 minutes")
            return minutes * 60
    if value.startswith("every ") and value.endswith(" minute"):
        raise ToolError(400, "Schedule must be at least 15 minutes")
    raise ToolError(400, "Unsupported schedule. Use 'every 30 minutes', 'hourly', or 'daily'.")


def webhook_static_error(url: str) -> Optional[str]:
    """Reject obvious non-public webhooks before any DNS lookup."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        return "Webhook URL must use https"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "Webhook URL must have a hostname"
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return "Webhook URL must be a public host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "Webhook URL must be a public host"
    return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _key(monitor_id: str) -> str:
    return f"c4ai:page-monitor:{monitor_id}"


class PageMonitor:
    def __init__(
        self,
        redis: Any,
        *,
        fetch: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        judge: Optional[Callable[[str, str, str], Awaitable[bool]]] = None,
        notify: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    ):
        self.redis = redis
        self.fetch = fetch
        self.judge = judge
        self.notify = notify or deliver_webhook

    async def create(
        self,
        *,
        name: str,
        urls: List[str],
        schedule: str,
        goal: Optional[str],
        webhook: Optional[str],
    ) -> Dict[str, Any]:
        if not name.strip():
            raise ToolError(400, "name is required")
        if not urls:
            raise ToolError(400, "At least one URL is required")
        if len(urls) > 20:
            raise ToolError(400, "At most 20 URLs")
        checked = [ensure_http_url(url) for url in urls]
        interval = parse_schedule(schedule)
        if webhook:
            problem = webhook_static_error(webhook)
            if problem:
                raise ToolError(400, problem)
        monitor_id = uuid.uuid4().hex[:16]
        now = time.time()
        record = {
            "id": monitor_id,
            "name": name.strip(),
            "urls": json.dumps(checked),
            "schedule": schedule,
            "interval": str(interval),
            "goal": goal or "",
            "webhook": webhook or "",
            "snapshots": "{}",
            "next_run": str(now + interval),
            "last_delivery": "",
        }
        await self.redis.hset(_key(monitor_id), mapping=record)
        await self.redis.sadd(INDEX_KEY, monitor_id)
        return {"id": monitor_id, "name": record["name"], "urls": checked, "schedule": schedule}

    async def list_monitors(self) -> Dict[str, Any]:
        ids = await self._ids()
        items = []
        for monitor_id in ids:
            record = await self._load(monitor_id)
            if record is None:
                continue
            items.append({
                "id": record["id"],
                "name": record["name"],
                "urls": json.loads(record["urls"]),
                "schedule": record["schedule"],
                "next_run": float(record["next_run"] or 0),
            })
        return {"monitors": items}

    async def delete(self, monitor_id: str) -> Dict[str, Any]:
        removed = await self.redis.delete(_key(monitor_id))
        await self.redis.srem(INDEX_KEY, monitor_id)
        if not removed:
            raise ToolError(404, "Monitor not found")
        return {"deleted": monitor_id}

    async def check(self, monitor_id: str) -> Dict[str, Any]:
        record = await self._load(monitor_id)
        if record is None:
            raise ToolError(404, "Monitor not found")
        if self.fetch is None:
            raise ToolError(500, "Page fetcher is not configured")
        urls = json.loads(record["urls"])
        snapshots = json.loads(record.get("snapshots") or "{}")
        pages = []
        for url in urls:
            try:
                current = await self.fetch(url)
            except Exception as exc:
                pages.append({"url": url, "status": "error", "detail": str(exc)[:200]})
                continue
            if current is None:
                pages.append({"url": url, "status": "removed"})
                snapshots.pop(url, None)
                continue
            normalized = normalize_snapshot(current)
            previous = snapshots.get(url)
            if previous is None:
                status = "new"
            elif previous == normalized:
                status = "same"
            else:
                real = True
                if record.get("goal") and self.judge is not None:
                    real = await self.judge(record["goal"], previous, normalized)
                status = "changed" if real else "same"
            snapshots[url] = normalized
            pages.append({"url": url, "status": status})
        record["snapshots"] = json.dumps(snapshots)
        record["next_run"] = str(time.time() + int(record["interval"]))
        notable = [page for page in pages if page["status"] != "same"]
        if notable and record.get("webhook"):
            payload = {"id": monitor_id, "name": record["name"], "pages": pages}
            try:
                await self.notify(record["webhook"], payload)
                record["last_delivery"] = "ok"
            except Exception as exc:
                record["last_delivery"] = f"error:{exc}"[:200]
                logger.warning("monitor webhook failed: %s", exc)
        await self.redis.hset(_key(monitor_id), mapping=record)
        return {"id": monitor_id, "pages": pages}

    async def due(self, now: float) -> List[str]:
        ready = []
        for monitor_id in await self._ids():
            record = await self._load(monitor_id)
            if record is None:
                continue
            if float(record.get("next_run") or 0) <= now:
                ready.append(monitor_id)
        return ready

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                for monitor_id in await self.due(time.time()):
                    await self.check(monitor_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("page monitor loop failed")

    async def _ids(self) -> List[str]:
        members = await self.redis.smembers(INDEX_KEY)
        return [_text(member) for member in members or []]

    async def _load(self, monitor_id: str) -> Optional[Dict[str, str]]:
        raw = await self.redis.hgetall(_key(monitor_id))
        if not raw:
            return None
        return {_text(key): _text(value) for key, value in raw.items()}


async def deliver_webhook(url: str, payload: Dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> None:
    async def _post(http: httpx.AsyncClient) -> None:
        response = await http.post(url, json=payload)
        response.raise_for_status()

    if client is not None:
        await _post(client)
        return
    async with httpx.AsyncClient(timeout=10) as http:
        await _post(http)


async def nanogpt_judge(goal: str, before: str, after: str) -> bool:
    """True when the change matters for the goal. Whitespace-only diffs never get here."""
    from nanogpt_client import complete

    answer = await complete([
        {
            "role": "user",
            "content": (
                "You judge a page change. Reply with exactly 'real' or 'ignore'. "
                "Reply ignore for whitespace, casing, encoding, timestamps, and tracking parameters. "
                f"Goal: {goal}\nBefore: {before[:4000]}\nAfter: {after[:4000]}"
            ),
        }
    ])
    return "ignore" not in (answer or "").lower()
