"""Hard timeout + stuck-aware health for the crawl concurrency limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.posture


@pytest.fixture
def fresh_guard(monkeypatch):
    """A ConcurrencyGuard isolated from the server singleton."""
    monkeypatch.setenv("CRAWL_HARD_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("CRAWL_HARD_TIMEOUT_MAX_SECONDS", "1")
    monkeypatch.setenv("CRAWL_HEALTH_STUCK_SECONDS", "0.05")
    monkeypatch.setenv("CRAWL_HEALTH_NO_COMPLETION_SECONDS", "0.05")
    monkeypatch.setenv("CRAWL_HEALTH_SLOTS_FULL_SECONDS", "0.05")
    from concurrency_guard import ConcurrencyGuard, request_hard_timeout

    request_hard_timeout.set(None)
    return ConcurrencyGuard(max_pages=2)


@pytest.mark.asyncio
async def test_slot_released_on_timeout_even_if_work_ignores_cancel(fresh_guard):
    """The Nov 2026 failure mode: cancel ignored, slot must still free."""
    started = asyncio.Event()
    released_after_timeout = asyncio.Event()

    async def sticky():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Swallow cancel briefly the way a stuck Playwright call can;
            # keep the second sleep short so the test process can exit.
            await asyncio.sleep(0.2)

    with pytest.raises(asyncio.TimeoutError):
        await fresh_guard.run(sticky, timeout=0.05)

    assert fresh_guard.slots_free == 2
    assert fresh_guard.in_flight == 0
    assert fresh_guard._last_completed_at is not None

    # A second crawl must be able to take the slot immediately.
    async def quick():
        released_after_timeout.set()
        return "ok"

    assert await fresh_guard.run(quick, timeout=1) == "ok"
    assert released_after_timeout.is_set()


@pytest.mark.asyncio
async def test_slot_released_on_exception(fresh_guard):
    async def boom():
        raise RuntimeError("proxy exhausted")

    with pytest.raises(RuntimeError, match="proxy exhausted"):
        await fresh_guard.run(boom, timeout=1)

    assert fresh_guard.slots_free == 2
    assert fresh_guard.in_flight == 0


@pytest.mark.asyncio
async def test_slot_released_on_cancel(fresh_guard):
    gate = asyncio.Event()

    async def blocked():
        gate.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(fresh_guard.run(blocked, timeout=30))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fresh_guard.slots_free == 2
    assert fresh_guard.in_flight == 0


@pytest.mark.asyncio
async def test_health_unhealthy_when_oldest_inflight_exceeds_threshold(fresh_guard, monkeypatch):
    monkeypatch.setenv("CRAWL_HEALTH_STUCK_SECONDS", "0.02")
    hold = asyncio.Event()

    async def linger():
        await hold.wait()
        return "done"

    task = asyncio.create_task(fresh_guard.run(linger, timeout=5))
    # Wait until the slot is taken.
    for _ in range(50):
        if fresh_guard.in_flight == 1:
            break
        await asyncio.sleep(0.01)
    assert fresh_guard.in_flight == 1

    await asyncio.sleep(0.05)
    assessment = fresh_guard.health_assessment()
    assert assessment["healthy"] is False
    assert assessment["in_flight"] == 1
    assert assessment["oldest_in_flight_seconds"] >= 0.02
    assert any("oldest in-flight" in r for r in assessment["reasons"])

    hold.set()
    assert await task == "done"

    # After completion, health recovers.
    assessment = fresh_guard.health_assessment()
    assert assessment["healthy"] is True
    assert assessment["in_flight"] == 0
    assert assessment["last_completed_at"] is not None


@pytest.mark.asyncio
async def test_health_unhealthy_when_all_slots_occupied_too_long(fresh_guard, monkeypatch):
    monkeypatch.setenv("CRAWL_HEALTH_SLOTS_FULL_SECONDS", "0.02")
    # Disable the oldest-in-flight check so this test isolates the full-slots rule.
    monkeypatch.setenv("CRAWL_HEALTH_STUCK_SECONDS", "999")
    holds = [asyncio.Event(), asyncio.Event()]

    async def linger(i):
        await holds[i].wait()

    tasks = [
        asyncio.create_task(fresh_guard.run(lambda i=i: linger(i), timeout=5))
        for i in range(2)
    ]
    for _ in range(50):
        if fresh_guard.in_flight == 2:
            break
        await asyncio.sleep(0.01)
    assert fresh_guard.in_flight == 2

    await asyncio.sleep(0.05)
    assessment = fresh_guard.health_assessment()
    assert assessment["healthy"] is False
    assert any("slots occupied" in r for r in assessment["reasons"])

    for ev in holds:
        ev.set()
    await asyncio.gather(*tasks)

    assert fresh_guard.health_assessment()["healthy"] is True


def test_resolve_hard_timeout_caps_override(monkeypatch):
    monkeypatch.setenv("CRAWL_HARD_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CRAWL_HARD_TIMEOUT_MAX_SECONDS", "200")
    from concurrency_guard import resolve_hard_timeout, request_hard_timeout

    request_hard_timeout.set(None)
    assert resolve_hard_timeout() == 120
    assert resolve_hard_timeout(50) == 50
    assert resolve_hard_timeout(999) == 200
    request_hard_timeout.set(180)
    assert resolve_hard_timeout() == 180
    request_hard_timeout.set(None)


def test_health_endpoint_includes_concurrency_and_503_when_stuck(stock_client, server_module, monkeypatch):
    monkeypatch.setenv("CRAWL_HEALTH_STUCK_SECONDS", "0.01")
    guard = server_module.get_guard()
    # Simulate a stuck in-flight crawl without running a real browser.
    token = "stuck-test"
    from concurrency_guard import FlightRecord
    import time as _time

    guard._in_flight[token] = FlightRecord(token, _time.monotonic() - 5.0, 120)
    try:
        r = stock_client.get("/health")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "unhealthy"
        assert body["in_flight"] >= 1
        assert "slots_total" in body
        assert "slots_free" in body
        assert "oldest_in_flight_seconds" in body
        assert body["oldest_in_flight_seconds"] >= 1
        assert body.get("reasons")
    finally:
        guard._in_flight.pop(token, None)

    r = stock_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "in_flight" in body
    assert "queued" in body
    assert "slots_total" in body
    assert "slots_free" in body
    assert "last_completed_at" in body
