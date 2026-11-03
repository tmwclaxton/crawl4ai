"""Retry a failed local crawl through DataImpulse, cheapest tier first.

A process-wide HTTP_PROXY would bill every page. Each retry instead sets a
process-wide upstream on the pinning proxy for the duration of one hop
(egress_proxy.use_request_upstream). Image SERPs are skipped. Firecrawl stays
last and stays off unless enabled.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from urllib.parse import quote, urlparse

from firecrawl_fallback import result_needs_fallback

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes"}
_TIERS: Tuple[Tuple[str, str, str], ...] = (
    ("datacenter", "DATAIMPULSE_DATACENTER_USERNAME", "DATAIMPULSE_DATACENTER_PASSWORD"),
    ("residential", "DATAIMPULSE_RESIDENTIAL_USERNAME", "DATAIMPULSE_RESIDENTIAL_PASSWORD"),
    ("premium", "DATAIMPULSE_PREMIUM_USERNAME", "DATAIMPULSE_PREMIUM_PASSWORD"),
)


def _flag(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower()


def proxy_fallback_enabled() -> bool:
    if _flag("CRAWL4AI_PROXY_FALLBACK", "true") not in _TRUE:
        return False
    return bool(configured_tiers())


# Hosts that recently returned a Cloudflare challenge. The next crawl of
# that host skips the local browser and goes straight to the proxy tiers.
_CLOUDFLARE_TTL_S = 30 * 60
_cloudflare_until: dict = {}


def remember_cloudflare(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host:
        _cloudflare_until[host] = time.time() + _CLOUDFLARE_TTL_S


def cloudflare_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and _cloudflare_until.get(host, 0) > time.time()


def partition_cloudflare_hosts(urls: List[str]) -> Tuple[List[str], List[str]]:
    """Split URLs into (crawl locally, skip straight to the proxy)."""
    local, proxied = [], []
    for url in urls:
        if cloudflare_host(url):
            proxied.append(url)
        else:
            local.append(url)
    return local, proxied


def cloudflare_placeholders(urls: List[str]) -> list:
    """Failed results so fill_failed_with_proxy sends these straight to DataImpulse."""
    return [
        {
            "url": url,
            "success": False,
            "html": "",
            "error_message": "Cloudflare block cached; skipping the local browser",
        }
        for url in urls
    ]


def mark_cloudflare_results(results: list) -> list:
    """A Cloudflare challenge page is not a successful crawl. Remember the host
    and let the proxy fallback take it."""
    try:
        from crawl4ai.antibot_detector import is_blocked
    except Exception:
        return results
    for result in results:
        if not isinstance(result, dict):
            continue
        html = result.get("html") or ""
        blocked, reason = is_blocked(result.get("status_code"), html, result.get("error_message"))
        if blocked and "Cloudflare" in (reason or ""):
            url = result.get("url")
            if isinstance(url, str):
                remember_cloudflare(url)
            result["success"] = False
            result["error_message"] = reason
    return results


def skip_paid_proxy(url: str) -> bool:
    """DuckDuckGo and Bing image results are shells. A paid hop does not help."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if "duckduckgo.com" in host and "ia=images" in query:
        return True
    if "bing.com" in host and "/images" in path:
        return True
    return False


def configured_tiers() -> List[Tuple[str, str]]:
    host = (os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com").strip()
    port = (os.environ.get("DATAIMPULSE_PORT") or "823").strip() or "823"
    tiers: List[Tuple[str, str]] = []
    for name, user_key, pass_key in _TIERS:
        username = (os.environ.get(user_key) or "").strip()
        password = (os.environ.get(pass_key) or "").strip()
        if not username or not password:
            continue
        user = quote(username, safe="")
        secret = quote(password, safe="")
        tiers.append((name, f"http://{user}:{secret}@{host}:{port}"))
    return tiers


def apply_proxy_result(result: dict, recovered: dict, tier: str) -> None:
    result.clear()
    result.update(recovered)
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["provider"] = f"crawl4ai-{tier}"
    result["metadata"] = metadata
    result["success"] = True
    result["error_message"] = None


async def fill_failed_with_proxy(
    results: list,
    crawl: Callable[[str], Awaitable[Optional[dict]]],
) -> list:
    """Replace failed or empty results by re-crawling through DataImpulse."""
    if not proxy_fallback_enabled():
        return results

    from egress_proxy import use_request_upstream

    tiers = configured_tiers()
    for result in results:
        if not result_needs_fallback(result):
            continue
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if skip_paid_proxy(url):
            continue
        import usage_log
        if not await usage_log.allow_paid("proxy"):
            logger.warning("DataImpulse daily cap spent; leaving %s unchanged", url)
            continue
        for name, proxy_url in tiers:
            recovered = None
            try:
                logger.warning("DataImpulse %s retrying %s", name, url)
                async with use_request_upstream(proxy_url):
                    recovered = await crawl(url)
            except Exception:
                logger.warning("DataImpulse %s retry failed for %s", name, url, exc_info=True)
            if not isinstance(recovered, dict) or result_needs_fallback(recovered):
                logger.warning("DataImpulse %s still blocked %s", name, url)
                continue
            apply_proxy_result(result, recovered, name)
            logger.warning("DataImpulse %s recovered %s", name, url)
            break
    return results
