"""
Per-call usage log for the GrantGunner Crawl4AI fork (crawl4ai:gg_fork).

Why: Crawl4AI only keeps the last 100 /crawl requests in memory (monitor.py) and running
totals per endpoint. It has no HTTP access log and does not record MCP tool calls, so calls
made straight to the server or its MCP are invisible to GrantGunner.

What it does:
  * An ASGI middleware records one entry per HTTP request: path, status, duration and the
    signed-in client (JWT "sub", set by auth_gate on request.state.principal).
  * record_mcp_tool() records one entry per MCP tool call: tool name, ok, duration.
  * Loopback calls the MCP bridge makes to itself (sub "mcp-service") are skipped, so a
    tool call is counted once, as the tool.
  * Entries go into the container's own Redis as a capped list (newest 50,000, a few MB).
  * GET /monitor/usage?after_id=N&limit=1000 returns entries with id > N, oldest first.
    It sits under /monitor, so the existing token gate protects it.

No URLs, request bodies or tokens are stored. Recording never breaks a request.
"""
import json
import os
import time
from contextvars import ContextVar
from typing import Optional

from fastapi import APIRouter, Query, Request

from auth import trusted_backend_subjects

# Who the current request is spending as. Set by the token rate-limit middleware
# after auth. Paid fallbacks read it so a daily cap is per token, not global.
current_client: ContextVar[str] = ContextVar("usage_current_client", default="anonymous")

# In-process fallback when Redis is down. Tests and a Redis blip still enforce
# the cap inside this process (gunicorn runs one worker).
_mem_spend: dict = {}

KEY_LIST = "usage:calls"
KEY_SEQ = "usage:seq"
MAX_ENTRIES = 50_000
SKIP_PREFIXES = ("/monitor", "/health", "/dashboard", "/static", "/playground", "/mcp", "/token", "/metrics", "/favicon")
LOOPBACK_SUBJECT = "mcp-service"

_redis = None


def init(redis) -> None:
    global _redis
    _redis = redis


async def _write(entry: dict) -> None:
    if _redis is None:
        return
    try:
        entry["id"] = int(await _redis.incr(KEY_SEQ))
        pipe = _redis.pipeline()
        pipe.lpush(KEY_LIST, json.dumps(entry, separators=(",", ":")))
        pipe.ltrim(KEY_LIST, 0, MAX_ENTRIES - 1)
        await pipe.execute()
    except Exception:  # never let logging break a request
        pass


def _subject(scope) -> Optional[str]:
    state = scope.get("state") or {}
    principal = state.get("principal") if isinstance(state, dict) else None
    if isinstance(principal, dict):
        sub = principal.get("sub") or principal.get("email")
        return str(sub)[:64] if sub else None
    return None


class UsageLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith(SKIP_PREFIXES):
            return await self.app(scope, receive, send)

        started = time.time()
        status = {"code": None}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status")
            await send(message)

        error = None
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error = type(exc).__name__
            status["code"] = status["code"] or 500
            raise
        finally:
            client = _subject(scope)
            if client != LOOPBACK_SUBJECT:
                code = status["code"]
                await _write({
                    "ts": round(started, 3),
                    "kind": "http",
                    "name": path[:64],
                    "status": code,
                    "ok": code is not None and code < 400,
                    "duration_ms": int((time.time() - started) * 1000),
                    "client": client,
                    "error": error or (f"HTTP {code}" if code and code >= 400 else None),
                })


async def record_mcp_tool(name: str, ok: bool, started: float, error: Optional[str] = None, client: Optional[str] = "mcp") -> None:
    await _write({
        "ts": round(started, 3),
        "kind": "mcp_tool",
        "name": name[:64],
        "status": None,
        "ok": ok,
        "duration_ms": int((time.time() - started) * 1000),
        "client": client,
        "error": (error or None) and str(error)[:200],
    })


def _client_skips_paid_cap(client: str) -> bool:
    """Trusted backend callers are not counted against the daily paid caps.

    The rate-limit middleware marks them with a trusted: prefix. A jwt:
    subject listed in CRAWL4AI_TRUSTED_SUBJECTS is skipped too, in case the
    cap is checked without that prefix. api_token: is the raw operator token.
    """
    if client.startswith("trusted:") or client.startswith("api_token:"):
        return True
    if client.startswith("jwt:"):
        sub = client.split(":", 1)[1].strip().lower()
        return bool(sub) and sub in trusted_backend_subjects()
    return False


def _paid_cap(kind: str) -> int:
    """Daily paid-call cap for one untrusted token. Negative means unlimited."""
    defaults = {"firecrawl": "500", "proxy": "1000", "nanogpt": "5000"}
    env_name = {
        "firecrawl": "CRAWL4AI_DAILY_FIRECRAWL_CAP",
        "proxy": "CRAWL4AI_DAILY_PROXY_CAP",
        "nanogpt": "CRAWL4AI_DAILY_NANOGPT_CAP",
    }.get(kind)
    raw = os.environ.get(env_name or "", defaults.get(kind, "0"))
    try:
        return int(str(raw).strip())
    except ValueError:
        return int(defaults.get(kind, "0"))


async def allow_paid(kind: str) -> bool:
    """Reserve one paid call for the current token today.

    False means the daily cap is spent. Callers skip the paid provider and
    return the local result, so a capped token still gets the free crawl.
    """
    cap = _paid_cap(kind)
    if cap < 0:
        return True
    client = current_client.get() or "anonymous"
    if _client_skips_paid_cap(client):
        return True
    day = time.strftime("%Y-%m-%d", time.gmtime())
    key = f"usage:spend:{client}:{day}:{kind}"
    if _redis is not None:
        try:
            count = int(await _redis.incr(key))
            if count == 1:
                await _redis.expire(key, 172800)
            if count > cap:
                await _redis.decr(key)
                return False
            return True
        except Exception:
            pass
    count = int(_mem_spend.get(key, 0)) + 1
    _mem_spend[key] = count
    return count <= cap


router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.get("/usage")
async def usage(request: Request, after_id: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=5000)):
    if _redis is None:
        return {"calls": []}
    raw = await _redis.lrange(KEY_LIST, 0, MAX_ENTRIES - 1)
    calls = []
    for item in raw:  # newest first
        try:
            entry = json.loads(item)
        except Exception:
            continue
        if entry.get("id", 0) <= after_id:
            break
        calls.append(entry)
    calls.reverse()
    return {"calls": calls[:limit]}
