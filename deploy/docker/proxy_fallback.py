"""Retry a failed local crawl through DataImpulse, cheapest tier first.

A process-wide HTTP_PROXY would bill every page. Each retry instead sets a
request-scoped upstream on the pinning proxy (egress_proxy.set_request_upstream).
Image SERPs are skipped. Firecrawl stays last and stays off unless enabled.
"""

from __future__ import annotations

import logging
import os
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

    from egress_proxy import reset_request_upstream, set_request_upstream

    tiers = configured_tiers()
    for result in results:
        if not result_needs_fallback(result):
            continue
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if skip_paid_proxy(url):
            continue
        for name, proxy_url in tiers:
            token = set_request_upstream(proxy_url)
            recovered = None
            try:
                recovered = await crawl(url)
            except Exception:
                logger.info("DataImpulse %s retry failed for %s", name, url, exc_info=True)
            finally:
                reset_request_upstream(token)
            if not isinstance(recovered, dict) or result_needs_fallback(recovered):
                continue
            apply_proxy_result(result, recovered, name)
            logger.info("DataImpulse %s recovered %s", name, url)
            break
    return results
