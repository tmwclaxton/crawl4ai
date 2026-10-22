"""Opt-in site_profile ("youtube"): consent cookies only when asked for,
youtube.com hosts only, and the default path left exactly as it was."""

import asyncio

import pytest

import page_cache
import site_profiles
from crawl4ai.async_configs import BrowserConfig, Provenance

API_KEY = "unit-test-api-token"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}
BODY_CFG = {"type": "CrawlerRunConfig", "params": {"cache_mode": "bypass"}}


@pytest.fixture
def captured(server_module, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", API_KEY)
    monkeypatch.setattr(page_cache, "cache_enabled", lambda: False)
    calls = []

    async def fake_handle(**kwargs):
        calls.append(kwargs)
        return {"success": True, "results": []}

    monkeypatch.setattr(server_module, "handle_crawl_request", fake_handle)
    return calls


def test_default_request_calls_the_crawler_exactly_as_before(stock_client, captured):
    r = stock_client.post("/crawl", json={"urls": ["https://www.youtube.com/@veritasium/videos"], "crawler_config": BODY_CFG}, headers=HEADERS)
    assert r.status_code == 200
    assert len(captured) == 1
    # same keyword set as before the feature existed: no site_profile key at all
    assert set(captured[0]) == {"urls", "browser_config", "crawler_config", "config", "hooks_config", "crawler_configs"}
    assert captured[0]["browser_config"] == {}
    assert captured[0]["crawler_config"] == BODY_CFG


def test_youtube_profile_is_passed_through(stock_client, captured):
    r = stock_client.post(
        "/crawl",
        json={"urls": ["https://www.youtube.com/@veritasium/videos", "https://youtube.com/@x"], "crawler_config": BODY_CFG, "site_profile": "YouTube"},
        headers=HEADERS,
    )
    assert r.status_code == 200
    assert captured[0]["site_profile"] == "youtube"


@pytest.mark.parametrize("url", [
    "https://example.com/",
    "https://evilyoutube.com/@x",
    "https://youtube.com.evil.org/@x",
    "https://m.youtube.co/@x",
])
def test_youtube_profile_rejects_other_hosts(stock_client, captured, url):
    r = stock_client.post("/crawl", json={"urls": ["https://www.youtube.com/@a", url], "site_profile": "youtube"}, headers=HEADERS)
    assert r.status_code == 400
    assert captured == []


def test_unknown_profile_is_rejected(stock_client, captured):
    r = stock_client.post("/crawl", json={"urls": ["https://www.youtube.com/@a"], "site_profile": "bogus"}, headers=HEADERS)
    assert r.status_code == 400
    assert captured == []


def test_body_still_cannot_set_cookies_with_the_profile(stock_client, captured, monkeypatch, server_module):
    # the real handler runs the untrusted loader, which must still refuse cookies
    import api
    monkeypatch.setattr(server_module, "handle_crawl_request", api.handle_crawl_request)
    r = stock_client.post(
        "/crawl",
        json={
            "urls": ["https://www.youtube.com/@a"],
            "site_profile": "youtube",
            "browser_config": {"type": "BrowserConfig", "params": {"cookies": [{"name": "x", "value": "y", "url": "https://www.youtube.com"}]}},
        },
        headers=HEADERS,
    )
    assert r.status_code in (400, 500)
    assert "SOCS" not in r.text


def _browser_seen_by_pool(monkeypatch, **kwargs):
    import api
    import crawler_pool
    seen = {}

    async def fake_get_crawler(cfg):
        seen["cfg"] = cfg
        raise RuntimeError("stop before launching a browser")

    monkeypatch.setattr(crawler_pool, "get_crawler", fake_get_crawler)
    monkeypatch.setattr(api, "_normalize_and_validate_seeds", lambda urls: urls)
    with pytest.raises(Exception):
        asyncio.run(api.handle_crawl_request(urls=["https://www.youtube.com/@a"], browser_config={}, crawler_config={}, config=api_config(), **kwargs))
    return seen["cfg"]


def api_config():
    import server
    return server.config


def test_default_browser_config_is_byte_identical(monkeypatch):
    baseline = BrowserConfig.load({}, provenance=Provenance.UNTRUSTED)
    from egress_broker import enforce_egress
    enforce_egress(baseline)
    cfg = _browser_seen_by_pool(monkeypatch)
    assert cfg.to_dict() == baseline.to_dict()
    assert not cfg.cookies
    assert "Accept-Language" not in (cfg.headers or {})


def test_youtube_profile_adds_consent_cookies_and_language(monkeypatch):
    cfg = _browser_seen_by_pool(monkeypatch, site_profile="youtube")
    names = {c["name"]: c for c in cfg.cookies}
    assert names["SOCS"]["value"] == site_profiles.YOUTUBE_SOCS
    assert names["SOCS"]["domain"] == ".youtube.com"
    assert names["CONSENT"]["value"] == site_profiles.YOUTUBE_CONSENT
    assert cfg.headers["Accept-Language"].startswith("en")


def test_apply_none_is_a_no_op():
    cfg = BrowserConfig()
    before = cfg.to_dict()
    site_profiles.apply_site_profile(None, cfg)
    assert cfg.to_dict() == before


def test_host_matcher():
    assert site_profiles.is_youtube_host("https://www.youtube.com/@a/videos")
    assert site_profiles.is_youtube_host("https://youtube.com/")
    assert site_profiles.is_youtube_host("https://m.youtube.com/watch?v=1")
    assert not site_profiles.is_youtube_host("https://youtu.be/1")
    assert not site_profiles.is_youtube_host("javascript:alert(1)")
    assert not site_profiles.is_youtube_host("https://notyoutube.com/")
