"""Optional Firecrawl backup and the GrantGunner speed caps.

Both are off unless the operator turns them on, except the page-timeout cap
and the same-domain pause, which default to the faster GrantGunner settings.
Set CRAWL4AI_PAGE_TIMEOUT_MS=60000 or CRAWL4AI_DISABLE_RATE_LIMITER=false
to restore the upstream behavior.

Firecrawl runs only after the local crawl has already failed or returned no
markdown. A normal page still comes from the browser and does not spend a
Firecrawl credit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes"}
# Successful pages in the 300-site run were about 0.8s. 15s still covers a
# slow render, and a stuck browser fails into the Firecrawl backup instead
# of sitting on the 60s library timeout.
_DEFAULT_PAGE_TIMEOUT_MS = 15_000
_FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"


def _flag(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower()


def page_timeout_cap_ms() -> int:
    """Upper bound applied to every crawl on this fork."""
    raw = _flag("CRAWL4AI_PAGE_TIMEOUT_MS", str(_DEFAULT_PAGE_TIMEOUT_MS))
    try:
        cap = int(raw)
    except ValueError:
        return _DEFAULT_PAGE_TIMEOUT_MS
    if cap <= 0:
        return _DEFAULT_PAGE_TIMEOUT_MS
    return cap


def rate_limiter_disabled() -> bool:
    """Skip the 1-2s pause between pages on the same host."""
    return _flag("CRAWL4AI_DISABLE_RATE_LIMITER", "true") in _TRUE


def firecrawl_fallback_enabled() -> bool:
    if _flag("CRAWL4AI_FIRECRAWL_FALLBACK", "false") not in _TRUE:
        return False
    return bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())


def apply_page_timeout_cap(config: Any) -> None:
    cap = page_timeout_cap_ms()
    configs = config if isinstance(config, list) else [config]
    for cfg in configs:
        current = getattr(cfg, "page_timeout", None)
        if current is None or int(current) > cap:
            cfg.page_timeout = cap


def markdown_text(result: dict) -> str:
    markdown = result.get("markdown")
    if isinstance(markdown, str):
        return markdown.strip()
    if isinstance(markdown, dict):
        for key in ("raw_markdown", "markdown_with_citations", "fit_markdown"):
            value = markdown.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def result_needs_fallback(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if not result.get("success"):
        return True
    return markdown_text(result) == ""


def apply_firecrawl_markdown(result: dict, markdown: str) -> None:
    result["success"] = True
    result["error_message"] = None
    result["markdown"] = {
        "raw_markdown": markdown,
        "markdown_with_citations": markdown,
        "references_markdown": "",
        "fit_markdown": None,
        "fit_html": None,
    }
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["provider"] = "firecrawl"
    result["metadata"] = metadata


def _post_firecrawl_sync(url: str) -> Optional[str]:
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        return None
    payload = json.dumps({"url": url, "formats": ["markdown"]}).encode()
    request = urllib.request.Request(
        _FIRECRAWL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        logger.info("Firecrawl backup HTTP %s for %s", exc.code, url)
        return None
    if status != 200:
        logger.info("Firecrawl backup HTTP %s for %s", status, url)
        return None
    body = json.loads(raw.decode() or "{}")
    data = body.get("data") if isinstance(body, dict) else None
    markdown = data.get("markdown") if isinstance(data, dict) else None
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    return None


async def _post_firecrawl(url: str) -> Optional[str]:
    return await asyncio.to_thread(_post_firecrawl_sync, url)


async def fill_failed_results(
    results: list,
    scrape: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
) -> list:
    """Replace failed or empty results with Firecrawl markdown when enabled."""
    if not firecrawl_fallback_enabled():
        return results
    scrape = scrape or _post_firecrawl
    for result in results:
        if not result_needs_fallback(result):
            continue
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        try:
            markdown = await scrape(url)
        except Exception:
            logger.info("Firecrawl backup failed for %s", url, exc_info=True)
            continue
        if not markdown:
            continue
        apply_firecrawl_markdown(result, markdown)
        logger.info("Firecrawl backup filled %s", url)
    return results
