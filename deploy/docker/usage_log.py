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
import time
from typing import Optional

from fastapi import APIRouter, Query, Request

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
