"""
concurrency_guard.py - hard-capped crawl slots that always release.

The Nov 2026 incident: hung crawls (stalled upstream proxy) held GLOBAL_SEM
permits forever. asyncio.wait_for cancels the work then *waits for that
cancel to finish*; when Playwright ignores CancelledError the wait never
ends and the slot leaks. This module:

  * owns the process-wide page semaphore
  * bounds every arun with a wall-clock hard timeout
  * releases the slot as soon as the deadline fires (does not wait for the
    cancelled task to finish)
  * tracks in-flight / queued / last-completion for stuck-aware /health
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Set
from uuid import uuid4

logger = logging.getLogger("crawl4ai.concurrency")

# Per-request override (seconds). Handlers set this when the body carries
# hard_timeout_seconds; capped_arun reads it.
request_hard_timeout: ContextVar[Optional[float]] = ContextVar(
    "request_hard_timeout", default=None
)

DEFAULT_HARD_TIMEOUT_S = 120.0
DEFAULT_HARD_TIMEOUT_MAX_S = 300.0
# Stuck thresholds default to 2x the hard timeout when env is unset.
DEFAULT_STUCK_MULTIPLIER = 2.0
DEFAULT_NO_COMPLETION_S = 180.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def hard_timeout_max_seconds() -> float:
    """Upper bound for any per-request override."""
    default_to = hard_timeout_default_seconds()
    configured = _env_float("CRAWL_HARD_TIMEOUT_MAX_SECONDS", DEFAULT_HARD_TIMEOUT_MAX_S)
    # Cap must be at least the default so the default is always legal.
    return max(configured, default_to)


def hard_timeout_default_seconds() -> float:
    """Process default for one crawl slot (env CRAWL_HARD_TIMEOUT_SECONDS)."""
    value = _env_float("CRAWL_HARD_TIMEOUT_SECONDS", DEFAULT_HARD_TIMEOUT_S)
    if value <= 0:
        return DEFAULT_HARD_TIMEOUT_S
    return value


def resolve_hard_timeout(override: Optional[float] = None) -> float:
    """Effective hard timeout: override (if any) clamped into (0, max]."""
    default = hard_timeout_default_seconds()
    cap = hard_timeout_max_seconds()
    chosen = override if override is not None else request_hard_timeout.get()
    if chosen is None:
        return min(default, cap)
    try:
        seconds = float(chosen)
    except (TypeError, ValueError):
        return min(default, cap)
    if seconds <= 0:
        return min(default, cap)
    return min(seconds, cap)


def stuck_oldest_threshold_seconds() -> float:
    return _env_float(
        "CRAWL_HEALTH_STUCK_SECONDS",
        hard_timeout_default_seconds() * DEFAULT_STUCK_MULTIPLIER,
    )


def no_completion_threshold_seconds() -> float:
    return _env_float(
        "CRAWL_HEALTH_NO_COMPLETION_SECONDS",
        DEFAULT_NO_COMPLETION_S,
    )


def slots_occupied_threshold_seconds() -> float:
    return _env_float(
        "CRAWL_HEALTH_SLOTS_FULL_SECONDS",
        hard_timeout_default_seconds() * DEFAULT_STUCK_MULTIPLIER,
    )


@dataclass
class FlightRecord:
    token: str
    started_at: float
    timeout_s: float


@dataclass
class ConcurrencyGuard:
    """Process-wide crawl-slot semaphore with hard timeout + health stats."""

    max_pages: int
    _sem: asyncio.Semaphore = field(init=False)
    _in_flight: Dict[str, FlightRecord] = field(default_factory=dict)
    _waiting: int = 0
    _last_completed_at: Optional[float] = None
    _slots_full_since: Optional[float] = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Prometheus gauges registered lazily (optional).
    _metrics_ready: bool = False

    def __post_init__(self) -> None:
        self.max_pages = max(1, int(self.max_pages))
        self._sem = asyncio.Semaphore(self.max_pages)

    @property
    def slots_free(self) -> int:
        # asyncio.Semaphore._value is the free-permit count.
        return int(getattr(self._sem, "_value", 0))

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)

    @property
    def queued(self) -> int:
        return self._waiting

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        mono = time.monotonic()
        oldest_age = 0.0
        if self._in_flight:
            oldest_start = min(r.started_at for r in self._in_flight.values())
            oldest_age = max(0.0, mono - oldest_start)
        last_completed = self._last_completed_at
        return {
            "slots_total": self.max_pages,
            "slots_free": self.slots_free,
            "in_flight": self.in_flight,
            "queued": self.queued,
            "oldest_in_flight_seconds": round(oldest_age, 3),
            "last_completed_at": last_completed,
            "seconds_since_last_completion": (
                None if last_completed is None else round(now - last_completed, 3)
            ),
            "slots_full_for_seconds": (
                None
                if self._slots_full_since is None
                else round(mono - self._slots_full_since, 3)
            ),
            "hard_timeout_seconds": hard_timeout_default_seconds(),
        }

    def health_assessment(self) -> Dict[str, Any]:
        """Return snapshot plus healthy bool and reason list."""
        snap = self.snapshot()
        reasons = []
        stuck_age = stuck_oldest_threshold_seconds()
        if snap["in_flight"] > 0 and snap["oldest_in_flight_seconds"] >= stuck_age:
            reasons.append(
                f"oldest in-flight crawl {snap['oldest_in_flight_seconds']:.1f}s "
                f">= stuck threshold {stuck_age:g}s"
            )
        no_done = no_completion_threshold_seconds()
        since = snap["seconds_since_last_completion"]
        if snap["queued"] > 0 and since is not None and since >= no_done:
            reasons.append(
                f"{snap['queued']} queued with no completion for {since:.1f}s "
                f"(threshold {no_done:g}s)"
            )
        # Also flag when everything is waiting and nothing has ever completed
        # since process start while work is queued.
        if snap["queued"] > 0 and since is None and snap["in_flight"] > 0:
            if snap["oldest_in_flight_seconds"] >= no_done:
                reasons.append(
                    f"{snap['queued']} queued and no completion recorded; "
                    f"oldest in-flight {snap['oldest_in_flight_seconds']:.1f}s"
                )
        full_for = snap["slots_full_for_seconds"]
        full_thresh = slots_occupied_threshold_seconds()
        if full_for is not None and full_for >= full_thresh:
            reasons.append(
                f"all {self.max_pages} slots occupied for {full_for:.1f}s "
                f">= threshold {full_thresh:g}s"
            )
        healthy = not reasons
        return {
            **snap,
            "healthy": healthy,
            "reasons": reasons,
        }

    async def run(
        self,
        work: Callable[[], Awaitable[Any]],
        *,
        timeout: Optional[float] = None,
        crawler: Any = None,
    ) -> Any:
        """Acquire a slot, run work under a hard deadline, always release."""
        timeout_s = resolve_hard_timeout(timeout)
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1

        token = uuid4().hex
        started = time.monotonic()
        async with self._lock:
            self._in_flight[token] = FlightRecord(token, started, timeout_s)
            if self.in_flight >= self.max_pages and self._slots_full_since is None:
                self._slots_full_since = started

        task = asyncio.create_task(work(), name=f"crawl-slot-{token[:8]}")
        try:
            # asyncio.wait returns when the deadline fires without waiting for
            # cancellation of the inner task (unlike wait_for). That is what
            # keeps the slot from leaking when Playwright ignores CancelledError.
            done, _ = await asyncio.wait({task}, timeout=timeout_s)
            if task in done:
                return task.result()
            task.cancel()
            await self._best_effort_release_browser(crawler, task)
            raise asyncio.TimeoutError(
                f"Crawl hard timeout after {timeout_s:g}s"
            )
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                await self._best_effort_release_browser(crawler, task)
            raise
        finally:
            self._sem.release()
            async with self._lock:
                self._in_flight.pop(token, None)
                if self.in_flight < self.max_pages:
                    self._slots_full_since = None
            self._last_completed_at = time.time()
            # Reap a cancelled zombie without blocking slot reuse.
            if not task.done():
                task.add_done_callback(_silence_task_exception)

    async def _best_effort_release_browser(self, crawler: Any, task: asyncio.Task) -> None:
        """Close open pages/contexts so a hung navigation cannot pin the pool."""
        if crawler is None:
            return
        try:
            strategy = getattr(crawler, "crawler_strategy", None)
            manager = getattr(strategy, "browser_manager", None) if strategy else None
            if manager is None:
                return
            pages: Set[Any] = set()
            getter = getattr(manager, "_get_pages_in_use", None)
            if callable(getter):
                with suppress(Exception):
                    pages.update(getter() or ())
            sessions = getattr(manager, "sessions", None) or {}
            for session in list(sessions.values()):
                page = session.get("page") if isinstance(session, dict) else getattr(session, "page", None)
                if page is not None:
                    pages.add(page)
            for page in pages:
                with suppress(Exception):
                    close = getattr(page, "close", None)
                    if callable(close):
                        await asyncio.wait_for(close(), timeout=2.0)
            # Drop session bookkeeping so the next caller does not reuse a dead page.
            if isinstance(sessions, dict):
                sessions.clear()
        except Exception:
            logger.debug("best-effort browser release failed", exc_info=True)


def _silence_task_exception(task: asyncio.Task) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.exception()


# Process-wide singleton installed by server.py at import time.
_GUARD: Optional[ConcurrencyGuard] = None


def init_guard(max_pages: int) -> ConcurrencyGuard:
    global _GUARD
    _GUARD = ConcurrencyGuard(max_pages=max_pages)
    return _GUARD


def get_guard() -> ConcurrencyGuard:
    if _GUARD is None:
        raise RuntimeError("ConcurrencyGuard not initialised")
    return _GUARD


def try_get_guard() -> Optional[ConcurrencyGuard]:
    return _GUARD
