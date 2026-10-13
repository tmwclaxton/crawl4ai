"""Shared page cache: expiry, size cap, profiles, and reuse of paid reads."""

import os

import page_cache
from page_cache import EXTERNAL_PROFILE, PageCache, external_result


API_KEY = "unit-test-api-token"


def _result(url, markdown, provider="crawl4ai"):
    result = external_result(url, markdown, provider)
    result["html"] = "<p>" + markdown + "</p>"
    return result


def _cache(tmp_path, **overrides):
    options = {"ttl_seconds": 3600, "max_bytes": 10_000_000, "purge_interval_seconds": 600}
    options.update(overrides)
    return PageCache(str(tmp_path / "pages.sqlite"), **options)


def test_stores_and_reads_a_page_until_it_expires(tmp_path):
    cache = _cache(tmp_path)
    assert cache.put("https://Example.com/grants/", "p1", _result("https://example.com/grants", "# Grants"), "crawl4ai", now=1000)
    hit = cache.get("https://example.com/grants", "p1", now=1500)
    assert hit["result"]["markdown"]["raw_markdown"] == "# Grants"
    assert hit["age_seconds"] == 500
    assert cache.get("https://example.com/grants", "p1", now=1000 + 3601) is None


def test_empty_pages_are_not_stored(tmp_path):
    cache = _cache(tmp_path)
    assert not cache.put("https://example.com/", "p1", _result("https://example.com/", "   "), "crawl4ai", now=1)
    assert cache.get("https://example.com/", "p1", now=2) is None


def test_profiles_stay_separate_but_paid_reads_are_shared(tmp_path):
    cache = _cache(tmp_path)
    cache.put("https://example.com/a", "image-serp", _result("https://example.com/a", "images"), "crawl4ai", now=10)
    assert cache.get("https://example.com/a", "plain", now=11) is None

    cache.put("https://example.com/b", EXTERNAL_PROFILE, _result("https://example.com/b", "# From Apify", "apify"), "apify", now=10)
    hit = cache.get("https://example.com/b", "plain", now=11)
    assert hit["provider"] == "apify"
    assert cache.get("https://example.com/b", "md:llm:summary:", external_fallback=False, now=11) is None


def test_any_profile_never_returns_a_filtered_markdown_copy(tmp_path):
    cache = _cache(tmp_path)
    cache.put("https://example.com/c", "md:llm:summary:", _result("https://example.com/c", "short summary"), "crawl4ai", now=10)
    assert cache.get("https://example.com/c", "md:raw::", any_profile=True, now=11) is None
    cache.put("https://example.com/c", "crawl-profile", _result("https://example.com/c", "# Whole page"), "crawl4ai", now=12)
    hit = cache.get("https://example.com/c", "md:raw::", any_profile=True, now=13)
    assert hit["result"]["markdown"]["raw_markdown"] == "# Whole page"


def test_purge_drops_expired_rows_and_least_recently_used_over_the_cap(tmp_path):
    cache = _cache(tmp_path, ttl_seconds=100, max_bytes=6_000, purge_interval_seconds=10_000)
    for index in range(6):
        noise = os.urandom(1000).hex()
        cache.put(f"https://example.com/{index}", "p", _result(f"https://example.com/{index}", noise), "crawl4ai", now=index)
    cache.get("https://example.com/0", "p", now=50)
    report = cache.purge(now=60)
    assert report["expired"] == 0
    assert report["evicted"] > 0
    assert cache.get("https://example.com/0", "p", now=61) is not None
    assert cache.get("https://example.com/1", "p", now=61) is None

    report = cache.purge(now=500)
    assert report["expired"] > 0
    assert cache.stats(now=500)["pages"] == 0


def test_forget_and_stats(tmp_path):
    cache = _cache(tmp_path)
    cache.put("https://example.com/d", "p", _result("https://example.com/d", "# D"), "crawl4ai", now=1)
    cache.put("https://example.com/e", EXTERNAL_PROFILE, _result("https://example.com/e", "# E", "firecrawl"), "firecrawl", now=1)
    cache.get("https://example.com/d", "p", now=2)
    cache.get("https://example.com/missing", "p", now=2)
    stats = cache.stats(now=3)
    assert stats["pages"] == 2
    assert stats["pages_by_provider"] == {"crawl4ai": 1, "firecrawl": 1}
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["stored_by_provider"]["firecrawl"] == 1
    assert cache.forget("https://example.com/d/") == 1
    assert cache.get("https://example.com/d", "p", now=4) is None


def test_cache_mode_and_profile_helpers():
    assert page_cache.reads_cache("enabled")
    assert not page_cache.reads_cache("bypass")
    assert page_cache.writes_cache("bypass")
    assert not page_cache.writes_cache("disabled")
    assert page_cache.profile_for({"excluded_tags": ["nav"], "cache_mode": "bypass"}) == page_cache.profile_for(
        {"excluded_tags": ["nav"], "cache_mode": "enabled"}
    )
    assert page_cache.profile_for({"excluded_tags": ["nav"]}) != page_cache.profile_for({"wait_for": "css:img"})
    assert not page_cache.cacheable_crawl({"screenshot": True})
    assert page_cache.cacheable_crawl({"excluded_tags": ["nav"]})


def test_crawl_route_serves_a_cached_page_without_crawling(stock_client, server_module, monkeypatch, tmp_path):
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", API_KEY)
    monkeypatch.setenv("CRAWL4AI_PAGE_CACHE_PATH", str(tmp_path / "route.sqlite"))
    monkeypatch.setattr(page_cache, "_cache", None)
    headers = {"Authorization": f"Bearer {API_KEY}"}

    stored = stock_client.post(
        "/cache/store",
        json={"url": "https://example.com/grant", "markdown": "# Paid read", "provider": "firecrawl"},
        headers=headers,
    )
    assert stored.status_code == 200
    assert stored.json()["stored"] is True

    async def must_not_crawl(**kwargs):
        raise AssertionError("crawl should come from the cache")

    monkeypatch.setattr(server_module, "handle_crawl_request", must_not_crawl)
    response = stock_client.post(
        "/crawl",
        json={
            "urls": ["https://example.com/grant"],
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {"type": "CrawlerRunConfig", "params": {"cache_mode": "enabled", "excluded_tags": ["nav", "footer"]}},
        },
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cache"]["hit"] is True
    assert body["results"][0]["metadata"]["provider"] == "firecrawl"
    assert body["results"][0]["markdown"]["raw_markdown"] == "# Paid read"

    stats = stock_client.get("/cache/stats", headers=headers).json()
    assert stats["pages_by_provider"]["firecrawl"] >= 1
    forgotten = stock_client.post("/cache/forget", json={"url": "https://example.com/grant"}, headers=headers)
    assert forgotten.json()["removed"] >= 1


def test_cache_routes_require_a_credential(stock_client):
    assert stock_client.get("/cache/stats").status_code == 401
    assert stock_client.post("/cache/store", json={}).status_code == 401
    assert stock_client.post("/cache/forget", json={}).status_code == 401
