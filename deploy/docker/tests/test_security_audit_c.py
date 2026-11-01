"""Section C of the GrantGunner audit: SSRF, rate limits, deploy identity.

These tests stay offline. DNS is the fake resolver from conftest.
"""

import asyncio
import socket
from pathlib import Path

import pytest

from auth import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from egress_broker import EgressBlocked, check_redirect, host_is_denied, resolve_and_pin
from mcp_oauth import ACCESS_TTL, REFRESH_TTL_SECONDS
from site_tools import ToolError, run_map

ROOT = Path(__file__).resolve().parents[3]
DOCKER = Path(__file__).resolve().parents[1]


def test_docker_hostname_is_blocked_before_dns():
    assert host_is_denied("grantgunner-app-1")
    assert host_is_denied("redis")
    assert host_is_denied("metadata.google.internal")
    assert not host_is_denied("example.com")
    with pytest.raises(EgressBlocked):
        resolve_and_pin("http://grantgunner-app-1/admin")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://127.1.2.3/",
    "http://10.1.2.3/",
    "http://172.16.0.5/",
    "http://172.31.255.255/",
    "http://192.168.1.9/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:127.0.0.1]/admin",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
])
def test_internal_literals_are_blocked(url):
    with pytest.raises(EgressBlocked):
        resolve_and_pin(url)


def test_public_literal_and_resolved_public_are_allowed(offline_dns):
    pin = resolve_and_pin("http://93.184.216.34/")
    assert pin.ip == "93.184.216.34"
    offline_dns.set("ok.example", "93.184.216.34")
    pin = resolve_and_pin("https://ok.example/docs")
    assert pin.ip == "93.184.216.34"


def test_dns_answer_in_a_private_range_is_blocked(offline_dns):
    offline_dns.set("looks-public.example", "10.2.3.4")
    with pytest.raises(EgressBlocked):
        resolve_and_pin("http://looks-public.example/")
    offline_dns.set("link.example", "169.254.169.254")
    with pytest.raises(EgressBlocked):
        resolve_and_pin("http://link.example/latest/meta-data/")


def test_redirect_to_an_internal_host_is_blocked():
    with pytest.raises(EgressBlocked):
        check_redirect("http://127.0.0.1/secret")
    with pytest.raises(EgressBlocked):
        check_redirect("http://grantgunner-app-1/")


def test_one_private_dns_answer_rejects_the_host(monkeypatch):
    def fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.4", port or 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    with pytest.raises(EgressBlocked):
        resolve_and_pin("http://mixed.example/")


def test_resolve_and_pin_uses_the_first_lookup_only(rebinding_dns):
    resolver = rebinding_dns("rebind.example", "93.184.216.34", "127.0.0.1")
    pin = resolve_and_pin("https://rebind.example/page")
    assert pin.ip == "93.184.216.34"
    assert resolver.calls == 1


def test_monitor_and_webhook_reject_a_docker_hostname():
    from page_monitor import PageMonitor, webhook_static_error

    assert webhook_static_error("https://grantgunner-app-1/hook")
    monitor = PageMonitor(object())
    with pytest.raises(ToolError) as caught:
        asyncio.run(monitor.create(
            name="inside",
            urls=["http://grantgunner-app-1/admin"],
            schedule="hourly",
            goal=None,
            webhook=None,
        ))
    assert caught.value.status == 400


def test_agent_rejects_an_explicit_internal_url():
    from site_tools import run_agent

    async def scrape(url):
        raise AssertionError("scrape must not run")

    async def complete(messages):
        raise AssertionError("complete must not run")

    with pytest.raises(ToolError) as caught:
        asyncio.run(run_agent(
            "read this",
            ["http://10.1.2.3/secret"],
            None,
            scrape=scrape,
            complete=complete,
        ))
    assert caught.value.status == 400


def test_run_map_rejects_an_internal_seed_without_calling_the_seeder():
    class Seeder:
        async def urls(self, domain, config):
            raise AssertionError("seeder must not run for an internal seed")

    with pytest.raises(ToolError) as caught:
        asyncio.run(run_map(
            "http://grantgunner-app-1",
            limit=5,
            search=None,
            sitemap="include",
            include_subdomains=False,
            seeder=Seeder(),
        ))
    assert caught.value.status == 400


def test_run_map_drops_internal_sitemap_links(offline_dns):
    class Seeder:
        async def urls(self, domain, config):
            return [
                {"url": "https://example.com/docs"},
                {"url": "http://127.0.0.1/secret"},
                {"url": "http://grantgunner-app-1/admin"},
                {"url": "http://10.0.0.5/"},
            ]

    result = asyncio.run(run_map(
        "https://example.com",
        limit=10,
        search=None,
        sitemap="include",
        include_subdomains=False,
        seeder=Seeder(),
    ))
    assert result["links"] == ["https://example.com/docs"]


def test_search_fetch_will_not_call_loopback():
    from tool_routes import _search_fetch

    status, _body = asyncio.run(_search_fetch("bing", "http://127.0.0.1:9/search", {}))
    assert status == 400


def test_img_img_wait_is_not_used_for_a_text_crawl():
    from crawl4ai.async_crawler_strategy import normalize_wait_for

    assert normalize_wait_for("css:img, img") is None
    assert normalize_wait_for("img, img") is None
    assert normalize_wait_for("css:img, img", screenshot=True) == "css:img"
    assert normalize_wait_for("css:#main, #main") == "css:#main"


def test_cloudflare_host_is_sent_straight_to_the_proxy():
    from proxy_fallback import (
        mark_cloudflare_results,
        partition_cloudflare_hosts,
        remember_cloudflare,
    )

    remember_cloudflare("https://blocked.example/first")
    local, proxied = partition_cloudflare_hosts([
        "https://blocked.example/next",
        "https://ok.example/",
    ])
    assert proxied == ["https://blocked.example/next"]
    assert local == ["https://ok.example/"]

    html = '<form class="challenge-form">__cf_chl_f_tk=abc</form>'
    marked = mark_cloudflare_results([{
        "url": "https://fresh.example/",
        "success": True,
        "status_code": 200,
        "html": html,
    }])
    assert marked[0]["success"] is False
    assert "Cloudflare" in marked[0]["error_message"]
    _local, again = partition_cloudflare_hosts(["https://fresh.example/2"])
    assert again == ["https://fresh.example/2"]


def test_paid_cap_is_per_token(monkeypatch):
    import usage_log

    monkeypatch.setattr(usage_log, "_redis", None)
    usage_log._mem_spend.clear()
    monkeypatch.setenv("CRAWL4AI_DAILY_FIRECRAWL_CAP", "1")
    first = usage_log.current_client.set("jwt:alice")
    try:
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is False
    finally:
        usage_log.current_client.reset(first)
    second = usage_log.current_client.set("jwt:bob")
    try:
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
    finally:
        usage_log.current_client.reset(second)


def test_jwt_lifetime_stays_short():
    assert ACCESS_TOKEN_EXPIRE_MINUTES <= 60
    assert ACCESS_TTL.total_seconds() <= 3600
    assert REFRESH_TTL_SECONDS <= 7 * 24 * 3600


def test_health_reports_the_image_git_sha(stock_client, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_GIT_SHA", "abc123def")
    body = stock_client.get("/health").json()
    assert body["status"] == "ok"
    assert body["git_sha"] == "abc123def"
    assert body["version"]


def test_shipped_browser_args_do_not_disable_tls(effective_browser_args):
    assert "--ignore-certificate-errors" not in effective_browser_args
    assert "--allow-insecure-localhost" not in effective_browser_args


def test_trusted_hosts_are_explicit(server_module):
    hosts = server_module.config["security"]["trusted_hosts"]
    assert hosts != ["*"]
    assert "crawl4ai.grantgunner.org" in hosts
    assert "localhost" in hosts


def test_jwt_callers_are_rate_limited_and_the_operator_token_is_not(stock_client, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_TOKEN_RATE_PER_MINUTE", "2")
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "operator-token-value")
    user = create_access_token({"sub": "mcp-user@example.com"})
    user_headers = {"Authorization": f"Bearer {user}"}
    user_codes = [stock_client.get("/schema", headers=user_headers).status_code for _ in range(3)]
    assert user_codes[0] != 429
    assert user_codes[2] == 429

    op_headers = {"Authorization": "Bearer operator-token-value"}
    op_codes = [stock_client.get("/schema", headers=op_headers).status_code for _ in range(3)]
    assert 429 not in op_codes


def test_compose_and_image_record_the_audit_fixes():
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    entry = (DOCKER / "entrypoint.sh").read_text()
    nginx = (DOCKER / "nginx-edge.conf").read_text()
    assert "mem_limit: 4g" in compose
    assert "CRAWL4AI_DISABLE_RATE_LIMITER=${CRAWL4AI_DISABLE_RATE_LIMITER:-false}" in compose
    assert "crawl4ai_redis:" in compose
    assert "REDIS_HOST=crawl4ai_redis" in compose
    assert "GIT_SHA" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert 'REDIS_HOST}" == "redis"' in entry or 'REDIS_HOST}" == "redis"' in entry.replace(" ", "")
    assert "refusing REDIS_HOST=redis" in entry
    assert "proxy_read_timeout 330s" in nginx
    assert "client_max_body_size 10m" in nginx
