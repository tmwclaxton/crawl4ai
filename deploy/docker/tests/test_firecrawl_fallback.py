"""Firecrawl backup and the GrantGunner speed caps. No browser, no network."""

import asyncio
import os

from firecrawl_fallback import (
    apply_firecrawl_markdown,
    apply_page_timeout_cap,
    fill_failed_results,
    firecrawl_fallback_enabled,
    page_timeout_cap_ms,
    rate_limiter_disabled,
    result_needs_fallback,
)


class _Cfg:
    def __init__(self, page_timeout):
        self.page_timeout = page_timeout


def test_page_timeout_defaults_to_15s(monkeypatch):
    monkeypatch.delenv("CRAWL4AI_PAGE_TIMEOUT_MS", raising=False)
    assert page_timeout_cap_ms() == 15_000
    cfg = _Cfg(60_000)
    apply_page_timeout_cap(cfg)
    assert cfg.page_timeout == 15_000
    already_fast = _Cfg(8_000)
    apply_page_timeout_cap(already_fast)
    assert already_fast.page_timeout == 8_000


def test_page_timeout_env_can_restore_60s(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_PAGE_TIMEOUT_MS", "60000")
    cfg = _Cfg(60_000)
    apply_page_timeout_cap(cfg)
    assert cfg.page_timeout == 60_000


def test_rate_limiter_is_off_unless_restored(monkeypatch):
    monkeypatch.delenv("CRAWL4AI_DISABLE_RATE_LIMITER", raising=False)
    assert rate_limiter_disabled() is True
    monkeypatch.setenv("CRAWL4AI_DISABLE_RATE_LIMITER", "false")
    assert rate_limiter_disabled() is False


def test_fallback_stays_off_without_a_key(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_FIRECRAWL_FALLBACK", "true")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert firecrawl_fallback_enabled() is False


def test_fallback_fills_a_failed_page_and_leaves_a_good_one(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_FIRECRAWL_FALLBACK", "true")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    async def fake(url: str):
        assert url == "https://blocked.example/"
        return "# recovered"

    results = asyncio.run(
        fill_failed_results(
            [
                {"url": "https://ok.example/", "success": True, "markdown": {"raw_markdown": "kept"}},
                {"url": "https://blocked.example/", "success": False, "error_message": "timeout", "markdown": ""},
            ],
            scrape=fake,
        )
    )

    assert results[0]["markdown"]["raw_markdown"] == "kept"
    assert "metadata" not in results[0] or results[0].get("metadata", {}).get("provider") != "firecrawl"
    assert results[1]["success"] is True
    assert results[1]["markdown"]["raw_markdown"] == "# recovered"
    assert results[1]["metadata"]["provider"] == "firecrawl"
    assert results[1]["error_message"] is None


def test_empty_success_needs_fallback_and_real_text_does_not():
    assert result_needs_fallback({"success": True, "markdown": {"raw_markdown": "  "}}) is True
    assert result_needs_fallback({"success": False, "markdown": "nope"}) is True
    assert result_needs_fallback({"success": True, "markdown": "hello"}) is False
    filled = {"success": False, "markdown": ""}
    apply_firecrawl_markdown(filled, "from firecrawl")
    assert result_needs_fallback(filled) is False


def test_fallback_does_not_call_out_when_disabled(monkeypatch):
    monkeypatch.setenv("CRAWL4AI_FIRECRAWL_FALLBACK", "false")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    async def boom(url: str):
        raise AssertionError(url)

    results = asyncio.run(
        fill_failed_results(
            [{"url": "https://blocked.example/", "success": False}],
            scrape=boom,
        )
    )
    assert results[0]["success"] is False
    assert "FIRECRAWL_API_KEY" not in os.environ or os.environ["FIRECRAWL_API_KEY"] == "test-key"
