"""Section C of the GrantGunner audit: SSRF, rate limits, deploy identity.

These tests stay offline. DNS is the fake resolver from conftest.
"""

import asyncio
import socket
from pathlib import Path

import pytest

from auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_token,
    is_trusted_backend,
)
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
    monkeypatch.delenv("CRAWL4AI_TRUSTED_SUBJECTS", raising=False)
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


def test_trusted_backend_skips_daily_paid_caps(monkeypatch):
    import usage_log

    monkeypatch.setattr(usage_log, "_redis", None)
    usage_log._mem_spend.clear()
    monkeypatch.setenv("CRAWL4AI_DAILY_FIRECRAWL_CAP", "1")
    monkeypatch.setenv("CRAWL4AI_DAILY_PROXY_CAP", "1")
    monkeypatch.setenv("CRAWL4AI_DAILY_NANOGPT_CAP", "1")
    monkeypatch.setenv("CRAWL4AI_TRUSTED_SUBJECTS", "backend@grantgunner.org")

    trusted = usage_log.current_client.set("trusted:backend@grantgunner.org")
    try:
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
        assert asyncio.run(usage_log.allow_paid("proxy")) is True
        assert asyncio.run(usage_log.allow_paid("nanogpt")) is True
    finally:
        usage_log.current_client.reset(trusted)

    named = usage_log.current_client.set("jwt:backend@grantgunner.org")
    try:
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
    finally:
        usage_log.current_client.reset(named)

    operator = usage_log.current_client.set("api_token:operator")
    try:
        assert asyncio.run(usage_log.allow_paid("proxy")) is True
        assert asyncio.run(usage_log.allow_paid("proxy")) is True
    finally:
        usage_log.current_client.reset(operator)

    other = usage_log.current_client.set("jwt:mcp-client")
    try:
        assert asyncio.run(usage_log.allow_paid("nanogpt")) is True
        assert asyncio.run(usage_log.allow_paid("nanogpt")) is False
    finally:
        usage_log.current_client.reset(other)


def test_negative_paid_cap_is_unlimited(monkeypatch):
    import usage_log

    monkeypatch.setattr(usage_log, "_redis", None)
    usage_log._mem_spend.clear()
    monkeypatch.setenv("CRAWL4AI_DAILY_FIRECRAWL_CAP", "-1")
    monkeypatch.delenv("CRAWL4AI_TRUSTED_SUBJECTS", raising=False)
    token = usage_log.current_client.set("jwt:mcp-client")
    try:
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
        assert asyncio.run(usage_log.allow_paid("firecrawl")) is True
    finally:
        usage_log.current_client.reset(token)


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
    assert "crawl4ai" in hosts
    assert "localhost" in hosts
    assert "127.0.0.1" in hosts
    assert "testserver" in hosts


def test_trusted_hosts_env_appends_extras(monkeypatch):
    from utils import load_config, resolve_trusted_hosts

    monkeypatch.setenv("CRAWL4AI_TRUSTED_HOSTS", "gg-agents, crawl4ai.internal, localhost")
    hosts = resolve_trusted_hosts([
        "crawl4ai.grantgunner.org",
        "crawl4ai",
        "localhost",
        "127.0.0.1",
        "testserver",
    ])
    assert hosts[:5] == [
        "crawl4ai.grantgunner.org",
        "crawl4ai",
        "localhost",
        "127.0.0.1",
        "testserver",
    ]
    assert "gg-agents" in hosts
    assert "crawl4ai.internal" in hosts
    assert hosts.count("localhost") == 1

    loaded = load_config()
    assert "gg-agents" in loaded["security"]["trusted_hosts"]
    assert "crawl4ai.grantgunner.org" in loaded["security"]["trusted_hosts"]
    assert "127.0.0.1" in loaded["security"]["trusted_hosts"]


def test_token_endpoint_stamps_operator_via(stock_client, server_module, monkeypatch):
    monkeypatch.setitem(server_module.config["security"], "api_token", "operator-token-value")
    monkeypatch.setattr(server_module, "verify_email_domain", lambda email: True)
    response = stock_client.post(
        "/token",
        json={"email": "backend@grantgunner.org", "api_token": "operator-token-value"},
    )
    assert response.status_code == 200
    claims = decode_token(response.json()["access_token"])
    assert claims["sub"] == "backend@grantgunner.org"
    assert claims["via"] == "operator"
    assert is_trusted_backend(claims) is True
    assert is_trusted_backend({"sub": "mcp-client", "scope": "data"}) is False


def test_jwt_callers_are_rate_limited_and_the_operator_token_is_not(stock_client, server_module, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_TOKEN_RATE_PER_MINUTE", "2")
    monkeypatch.delenv("CRAWL4AI_TRUSTED_SUBJECTS", raising=False)
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "operator-token-value")
    server_module._token_hits.clear()
    user = create_access_token({"sub": "mcp-user@example.com"})
    user_headers = {"Authorization": f"Bearer {user}"}
    user_codes = [stock_client.get("/schema", headers=user_headers).status_code for _ in range(3)]
    assert user_codes[0] != 429
    assert user_codes[2] == 429

    op_headers = {"Authorization": "Bearer operator-token-value"}
    op_codes = [stock_client.get("/schema", headers=op_headers).status_code for _ in range(3)]
    assert 429 not in op_codes


def test_operator_issued_jwt_is_exempt_from_the_per_minute_cap(stock_client, server_module, monkeypatch):
    """60 or more requests from the backend subject must not 429.

    GrantGunner swaps the operator token for this JWT at POST /token and
    then sends it on every scrape and search call.
    """
    monkeypatch.setenv("CRAWL4AI_TOKEN_RATE_PER_MINUTE", "60")
    monkeypatch.delenv("CRAWL4AI_TRUSTED_SUBJECTS", raising=False)
    server_module._token_hits.clear()
    backend = create_access_token({"sub": "backend@grantgunner.org", "via": "operator"})
    headers = {"Authorization": f"Bearer {backend}"}
    codes = [stock_client.get("/schema", headers=headers).status_code for _ in range(61)]
    assert 429 not in codes
    assert codes[0] != 401


def test_named_trusted_subject_is_exempt_and_other_subjects_are_not(stock_client, server_module, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_TOKEN_RATE_PER_MINUTE", "2")
    monkeypatch.setenv("CRAWL4AI_TRUSTED_SUBJECTS", "Backend@GrantGunner.org")
    server_module._token_hits.clear()
    trusted = create_access_token({"sub": "backend@grantgunner.org"})
    trusted_codes = [
        stock_client.get("/schema", headers={"Authorization": f"Bearer {trusted}"}).status_code
        for _ in range(5)
    ]
    assert 429 not in trusted_codes

    other = create_access_token({"sub": "mcp-client"})
    other_codes = [
        stock_client.get("/schema", headers={"Authorization": f"Bearer {other}"}).status_code
        for _ in range(3)
    ]
    assert other_codes[0] != 429
    assert other_codes[2] == 429


def test_negative_token_rate_is_unlimited(stock_client, server_module, monkeypatch):
    monkeypatch.setenv("CRAWL4AI_TOKEN_RATE_PER_MINUTE", "-1")
    monkeypatch.delenv("CRAWL4AI_TRUSTED_SUBJECTS", raising=False)
    server_module._token_hits.clear()
    user = create_access_token({"sub": "mcp-user@example.com"})
    headers = {"Authorization": f"Bearer {user}"}
    codes = [stock_client.get("/schema", headers=headers).status_code for _ in range(5)]
    assert 429 not in codes


def test_compose_and_image_record_the_audit_fixes():
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    entry = (DOCKER / "entrypoint.sh").read_text()
    nginx = (DOCKER / "nginx-edge.conf").read_text()
    assert "mem_limit: 4g" in compose
    assert "CRAWL4AI_DISABLE_RATE_LIMITER=${CRAWL4AI_DISABLE_RATE_LIMITER:-false}" in compose
    assert "CRAWL4AI_TOKEN_RATE_PER_MINUTE=${CRAWL4AI_TOKEN_RATE_PER_MINUTE:-60}" in compose
    assert "CRAWL4AI_TRUSTED_SUBJECTS=${CRAWL4AI_TRUSTED_SUBJECTS:-}" in compose
    assert "CRAWL4AI_TRUSTED_HOSTS=${CRAWL4AI_TRUSTED_HOSTS:-}" in compose
    assert "CRAWL4AI_DAILY_FIRECRAWL_CAP=${CRAWL4AI_DAILY_FIRECRAWL_CAP:-500}" in compose
    assert "CRAWL4AI_DAILY_PROXY_CAP=${CRAWL4AI_DAILY_PROXY_CAP:-1000}" in compose
    assert "CRAWL4AI_DAILY_NANOGPT_CAP=${CRAWL4AI_DAILY_NANOGPT_CAP:-5000}" in compose
    assert "Negative means unlimited" in compose
    assert "crawl4ai_redis:" in compose
    assert "REDIS_HOST=crawl4ai_redis" in compose
    assert "local-dev-crawl4ai-redis" not in compose
    assert "${REDIS_PASSWORD:?" in compose
    assert "volatile-lru" in compose
    assert "allkeys-lru" not in compose
    assert "REDIS_MAXMEMORY:-1gb" in compose
    assert "crawl4ai_redis_data" in compose
    assert "GIT_SHA" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert 'REDIS_HOST}" == "redis"' in entry or 'REDIS_HOST}" == "redis"' in entry.replace(" ", "")
    assert "refusing REDIS_HOST=redis" in entry
    assert "REDIS_PASSWORD is required when REDIS_HOST" in entry
    assert "There is no default password" in entry
    assert "proxy_read_timeout 330s" in nginx
    assert "client_max_body_size 10m" in nginx
