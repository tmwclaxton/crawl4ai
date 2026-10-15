"""DataImpulse retry after a failed local crawl. No browser, no network."""

import asyncio
import os

import egress_proxy
from egress_proxy import reset_request_upstream, set_request_upstream
from proxy_fallback import (
    apply_proxy_result,
    configured_tiers,
    fill_failed_with_proxy,
    proxy_fallback_enabled,
    skip_paid_proxy,
)


def _clear_dataimpulse(monkeypatch):
    for name in (
        "CRAWL4AI_PROXY_FALLBACK",
        "DATAIMPULSE_HOST",
        "DATAIMPULSE_PORT",
        "DATAIMPULSE_DATACENTER_USERNAME",
        "DATAIMPULSE_DATACENTER_PASSWORD",
        "DATAIMPULSE_RESIDENTIAL_USERNAME",
        "DATAIMPULSE_RESIDENTIAL_PASSWORD",
        "DATAIMPULSE_PREMIUM_USERNAME",
        "DATAIMPULSE_PREMIUM_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_tiers(monkeypatch, *, datacenter=True, residential=True, premium=True):
    monkeypatch.setenv("DATAIMPULSE_HOST", "gw.dataimpulse.com")
    monkeypatch.setenv("DATAIMPULSE_PORT", "823")
    if datacenter:
        monkeypatch.setenv("DATAIMPULSE_DATACENTER_USERNAME", "dc-user")
        monkeypatch.setenv("DATAIMPULSE_DATACENTER_PASSWORD", "dc-pass")
    if residential:
        monkeypatch.setenv("DATAIMPULSE_RESIDENTIAL_USERNAME", "res-user")
        monkeypatch.setenv("DATAIMPULSE_RESIDENTIAL_PASSWORD", "res-pass")
    if premium:
        monkeypatch.setenv("DATAIMPULSE_PREMIUM_USERNAME", "prem-user")
        monkeypatch.setenv("DATAIMPULSE_PREMIUM_PASSWORD", "prem-pass")


def test_stays_off_without_credentials(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    assert proxy_fallback_enabled() is False
    assert configured_tiers() == []


def test_stays_off_when_flag_is_false(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    _set_tiers(monkeypatch)
    monkeypatch.setenv("CRAWL4AI_PROXY_FALLBACK", "false")
    assert proxy_fallback_enabled() is False


def test_skips_image_serps():
    assert skip_paid_proxy("https://duckduckgo.com/?q=grant&ia=images&iax=images") is True
    assert skip_paid_proxy("https://www.bing.com/images/search?q=grant") is True
    assert skip_paid_proxy("https://www.gov.il/he/pages/omanut") is False


def test_request_scoped_upstream_beats_env(monkeypatch):
    monkeypatch.delenv("CRAWL4AI_UPSTREAM_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://env-proxy:3128")
    token = set_request_upstream("http://dc-user:dc-pass@gw.dataimpulse.com:823")
    try:
        host, port, auth = egress_proxy.upstream_proxy()
        assert host == "gw.dataimpulse.com"
        assert port == 823
        assert auth is not None
    finally:
        reset_request_upstream(token)
    assert egress_proxy.upstream_proxy() == ("env-proxy", 3128, None)


def test_recovers_on_datacenter_and_leaves_a_good_page(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    _set_tiers(monkeypatch)
    seen = []

    async def crawl(url: str):
        seen.append((url, egress_proxy.upstream_proxy()[0] if egress_proxy.upstream_proxy() else None))
        return {"url": url, "success": True, "markdown": {"raw_markdown": "# recovered"}}

    results = asyncio.run(
        fill_failed_with_proxy(
            [
                {"url": "https://ok.example/", "success": True, "markdown": {"raw_markdown": "kept"}},
                {"url": "https://blocked.example/", "success": False, "error_message": "403", "markdown": ""},
            ],
            crawl,
        )
    )

    assert results[0]["markdown"]["raw_markdown"] == "kept"
    assert results[0].get("metadata", {}).get("provider") != "crawl4ai-datacenter"
    assert results[1]["success"] is True
    assert results[1]["markdown"]["raw_markdown"] == "# recovered"
    assert results[1]["metadata"]["provider"] == "crawl4ai-datacenter"
    assert results[1]["error_message"] is None
    assert seen == [("https://blocked.example/", "gw.dataimpulse.com")]


def test_escalates_when_cheaper_tier_still_fails(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    _set_tiers(monkeypatch)
    seen = []

    async def crawl(url: str):
        host, port, auth = egress_proxy.upstream_proxy()
        import base64
        user = base64.b64decode(auth.split(b" ")[-1].strip()).split(b":")[0].decode()
        seen.append(user)
        if user == "dc-user":
            return {"url": url, "success": False, "markdown": ""}
        return {"url": url, "success": True, "markdown": "ok via residential"}

    results = asyncio.run(
        fill_failed_with_proxy(
            [{"url": "https://blocked.example/", "success": False}],
            crawl,
        )
    )
    assert seen == ["dc-user", "res-user"]
    assert results[0]["metadata"]["provider"] == "crawl4ai-residential"
    assert results[0]["success"] is True


def test_skips_image_serp_without_calling_crawl(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    _set_tiers(monkeypatch)

    async def boom(url: str):
        raise AssertionError(url)

    results = asyncio.run(
        fill_failed_with_proxy(
            [{"url": "https://duckduckgo.com/?q=grant&ia=images", "success": False}],
            boom,
        )
    )
    assert results[0]["success"] is False


def test_does_not_call_out_when_disabled(monkeypatch):
    _clear_dataimpulse(monkeypatch)
    _set_tiers(monkeypatch)
    monkeypatch.setenv("CRAWL4AI_PROXY_FALLBACK", "false")

    async def boom(url: str):
        raise AssertionError(url)

    results = asyncio.run(
        fill_failed_with_proxy(
            [{"url": "https://blocked.example/", "success": False}],
            boom,
        )
    )
    assert results[0]["success"] is False


def test_apply_proxy_result_sets_provider():
    result = {"url": "https://a.example/", "success": False}
    apply_proxy_result(result, {"url": "https://a.example/", "success": True, "markdown": "hi"}, "premium")
    assert result["metadata"]["provider"] == "crawl4ai-premium"
    assert result["markdown"] == "hi"
