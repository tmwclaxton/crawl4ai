"""Offline tests for the total /md fallback budget."""

import asyncio
import inspect
import json
import time

import pytest
from fastapi import HTTPException


def test_md_budget_times_out(monkeypatch, server_module, caplog):
    import page_cache

    monkeypatch.setenv("MD_TOTAL_BUDGET_S", "0.02")
    monkeypatch.setattr(page_cache, "cache_enabled", lambda: False)
    cancelled = []

    async def slow_markdown(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(server_module, "handle_markdown_request", slow_markdown)
    route = inspect.unwrap(server_module.get_markdown)
    body = server_module.MarkdownRequest(url="https://example.com/")
    start = time.monotonic()
    with pytest.raises(HTTPException) as error:
        asyncio.run(route(request=None, body=body, _td={}))
    elapsed = time.monotonic() - start

    assert error.value.status_code == 504
    assert error.value.detail == (
        "md budget exceeded after 0.02s (browser, http and proxy tiers)"
    )
    assert 0.02 <= elapsed < 1
    assert cancelled == [True]
    assert body.url in caplog.text


@pytest.mark.parametrize("value, expected_timeout", [("invalid", 28), ("0", None), ("-1", None)])
def test_md_budget_configuration(monkeypatch, server_module, value, expected_timeout):
    import page_cache

    monkeypatch.setenv("MD_TOTAL_BUDGET_S", value)
    monkeypatch.setattr(page_cache, "cache_enabled", lambda: False)
    timeouts = []

    async def markdown(*args):
        return "markdown", "http"

    async def wait_for(coro, *, timeout):
        timeouts.append(timeout)
        return await coro

    monkeypatch.setattr(server_module, "handle_markdown_request", markdown)
    monkeypatch.setattr(server_module.asyncio, "wait_for", wait_for)
    route = inspect.unwrap(server_module.get_markdown)
    body = server_module.MarkdownRequest(url="https://example.com/")
    response = asyncio.run(route(request=None, body=body, _td={}))

    assert timeouts == [expected_timeout]
    assert json.loads(response.body)["markdown"] == "markdown"
