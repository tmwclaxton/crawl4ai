"""Offline coverage for concurrent search and request usage accounting."""

import asyncio
import json
import time

import httpx
import pytest

from llm_usage import LLMUsageMiddleware, usage_header
from nanogpt_client import complete, request_usage
from site_tools import engine_names, reset_search_state, run_search


def test_search_fetch_and_judge_concurrent():
    async def run():
        reset_search_state()
        judged = []

        async def fetch(name, url, headers):
            await asyncio.sleep(0.2)
            return 200, name

        async def judge(messages):
            judged.append(messages)
            await asyncio.sleep(0.2)
            return "YES"

        start = time.monotonic()
        result = await run_search("query", limit=10, sources="web", categories=None, fetch=fetch, judge=judge)
        return result, judged, time.monotonic() - start

    # Use the real relevance path with a simple parser shared by all engines.
    from unittest.mock import patch
    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        result, judged, elapsed = asyncio.run(run())
    assert result["engines_used"] == engine_names()
    assert len(judged) == 4
    assert elapsed < 0.7
    assert [hit["engine"] for hit in result["data"]["web"]] == engine_names()


def test_search_budget_preserves_finished_and_cancels_pending(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "0.05")
    from unittest.mock import patch

    async def run():
        reset_search_state()
        cancelled = []

        async def fetch(name, url, headers):
            if name != "bing":
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    cancelled.append(name)
                    raise
            return 200, "body"

        result = await run_search("query", limit=1, sources="web", categories=None, fetch=fetch)
        assert len(cancelled) == 3
        return result

    with patch("site_tools.parse_search_body", return_value=[{"url": "https://example.com/", "title": "query"}]):
        result = asyncio.run(run())
    assert result["engines_used"] == ["bing"]
    assert len(result["engines_skipped"]) == 3
    assert result["data"]["web"]


def test_apify_skipped_when_fallback_false(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "1")
    from unittest.mock import patch

    calls = []

    async def fetch(name, url, headers, payload=None):
        calls.append(name)
        return 200, name

    async def run():
        reset_search_state()
        return await run_search(
            "query", limit=10, sources="web", categories=None,
            apify_token="tok", fetch=fetch, fallback=False,
        )

    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        result = asyncio.run(run())

    assert "apify" not in calls
    assert "apify" not in result["engines_used"]
    assert all(item["engine"] != "apify" for item in result["engines_skipped"])


def test_apify_skipped_with_deadline_reason_when_time_short(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "1")
    from unittest.mock import patch

    calls = []

    async def fetch(name, url, headers, payload=None):
        calls.append(name)
        if name == "apify":
            await asyncio.sleep(5)
        return 200, name

    async def run():
        reset_search_state()
        return await run_search(
            "query", limit=10, sources="web", categories=None,
            apify_token="tok", fetch=fetch, fallback=True, deadline_s=0.05,
        )

    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        start = time.monotonic()
        result = asyncio.run(run())
        elapsed = time.monotonic() - start

    assert "apify" not in calls
    assert {"engine": "apify", "reason": "deadline"} in result["engines_skipped"]
    assert elapsed < 1


def test_apify_call_cut_at_deadline(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "1")
    monkeypatch.setenv("SEARCH_APIFY_MIN_S", "0")
    from unittest.mock import patch

    calls = []

    async def fetch(name, url, headers, payload=None):
        calls.append(name)
        if name == "apify":
            await asyncio.sleep(5)
        return 200, name

    async def run():
        reset_search_state()
        return await run_search(
            "query", limit=10, sources="web", categories=None,
            apify_token="tok", fetch=fetch, fallback=True, deadline_s=0.2,
        )

    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        start = time.monotonic()
        result = asyncio.run(run())
        elapsed = time.monotonic() - start

    assert calls.count("apify") == 1
    assert {"engine": "apify", "reason": "deadline"} in result["engines_skipped"]
    assert elapsed < 1


def test_low_relevance_hits_returned_when_nothing_else(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "1")
    from unittest.mock import patch

    async def fetch(name, url, headers, payload=None):
        return 200, name

    async def judge(messages):
        return "NO"

    async def run():
        reset_search_state()
        return await run_search(
            "query", limit=5, sources="web", categories=None,
            fetch=fetch, judge=judge,
        )

    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        result = asyncio.run(run())

    hits = result["data"]["web"]
    assert hits
    assert all(hit.get("low_relevance") is True for hit in hits)
    assert all(item["reason"] == "irrelevant" for item in result["engines_skipped"])


def test_elapsed_ms_present(monkeypatch):
    monkeypatch.setenv("SEARCH_FREE_BUDGET_S", "1")
    from unittest.mock import patch

    async def fetch(name, url, headers, payload=None):
        return 200, name

    async def run():
        reset_search_state()
        return await run_search("query", limit=3, sources="web", categories=None, fetch=fetch)

    with patch("site_tools.parse_search_body", side_effect=lambda name, body: [
        {"url": f"https://{name}.example/", "title": "query", "description": "query", "engine": name}
    ]):
        result = asyncio.run(run())

    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0


def test_accumulator_and_middleware(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    async def run():
        bodies = [
            {"cost": 0.1, "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            {"usage": {"prompt_tokens": 4, "completion_tokens": 1, "cost": 0.2}},
        ]

        def respond(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], **bodies.pop(0)})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async def app(scope, receive, send):
                assert request_usage.get() == []
                assert await complete([], model="test-model", client=client) == "ok"
                await asyncio.create_task(complete([], model="test-model", client=client))
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            sent = []

            async def send(message):
                sent.append(message)

            await LLMUsageMiddleware(app)({"type": "http"}, None, send)
            assert request_usage.get() is None
            summary = json.loads(dict(sent[0]["headers"])[b"x-c4-llm-usage"])
            assert summary["calls"] == 2
            assert summary["prompt_tokens"] == 7
            assert summary["completion_tokens"] == 3
            assert summary["cost"] == pytest.approx(0.3)
            assert summary["models"]["test-model"]["calls"] == 2

    asyncio.run(run())


def test_usage_missing_cost_and_header_bound():
    calls = [{"model": "model" + str(i) * 100, "prompt_tokens": 1, "completion_tokens": 2, "cost": None}
             for i in range(100)]
    value = usage_header(calls)
    assert len(value) < 2000
    assert json.loads(value)["cost"] is None
    assert json.loads(value)["calls"] == 100


def test_usage_empty_and_broken_accounting_never_break_request():
    async def run(calls):
        async def app(scope, receive, send):
            request_usage.get().extend(calls)
            await send({"type": "http.response.start", "status": 200, "headers": []})

        sent = []

        async def send(message):
            sent.append(message)

        await LLMUsageMiddleware(app)({"type": "http"}, None, send)
        assert sent[0]["headers"] == []
        assert request_usage.get() is None

    asyncio.run(run([]))
    asyncio.run(run([{}]))


def test_accumulator_ignores_failures_and_isolates_requests(monkeypatch):
    from nanogpt_client import NanoGPTError
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    async def run():
        async def one(model):
            token = request_usage.set([])
            try:
                def respond(request):
                    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    await complete([], model=model, client=client)
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
                    with pytest.raises(NanoGPTError):
                        await complete([], model=model, client=client)
                calls = request_usage.get()
                assert len(calls) == 1
                assert calls[0] == {"model": model, "prompt_tokens": 0, "completion_tokens": 0, "cost": None}
            finally:
                request_usage.reset(token)

        await asyncio.gather(one("first"), one("second"))
        assert request_usage.get() is None

    asyncio.run(run())
