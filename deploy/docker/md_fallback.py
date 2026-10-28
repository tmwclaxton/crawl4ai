"""Fallback paths for POST /md when the browser crawl can't reach a page.

Three failure modes show up in production (all surfaced to callers as a
502 from the browser crawl):

  - Documents (.pdf/.docx/.doc/...): Chromium cannot navigate to a file
    download ("Page.goto: net::ERR_FAILED", "ACS-GOTO", "Download is
    starting"). A plain HTTP GET of the same URL works fine.
  - Anti-bot / rate limiting (HTTP 429, 403): a plain GET with an ordinary
    desktop User-Agent often succeeds where a fingerprinted headless browser
    is blocked.
  - Egress routing (net::ERR_TUNNEL_CONNECTION_FAILED and friends): the
    container's direct route to the target is broken; only a different
    egress IP (the DataImpulse proxy tiers) gets there at all.

Every outbound request made here carries the same SSRF guarantees as the
browser path: the initial URL and every redirect Location are re-validated
with egress_broker, and the request is routed through the pinning proxy
(egress_broker.get_egress_proxy()) when one is running - see plain_http_get.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from egress_broker import EgressBlocked, check_redirect, get_egress_proxy, resolve_and_pin
from utils import FilterType

logger = logging.getLogger(__name__)

DOCUMENT_SUFFIXES = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".odt", ".rtf"}

_CONTENT_TYPE_SUFFIXES = (
    ("application/pdf", ".pdf"),
    ("application/msword", ".doc"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ("application/vnd.ms-excel", ".xls"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ("application/vnd.oasis.opendocument.text", ".odt"),
    ("application/rtf", ".rtf"),
    ("text/rtf", ".rtf"),
)

NAVIGATION_FAILURE_MARKERS = (
    "ERR_FAILED",
    "ERR_ABORTED",
    "Download is starting",
    "ACS-GOTO",
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 20.0
MAX_RETRY_AFTER_SECONDS = 5.0


def url_suffix(url: str) -> str:
    """The lowercased file extension of the URL's path, or "" if none."""
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot >= 0 and dot > path.rfind("/") else ""


def is_document_url(url: str) -> bool:
    return url_suffix(url) in DOCUMENT_SUFFIXES


def content_type_suffix(content_type: Optional[str]) -> Optional[str]:
    """Map a response Content-Type to a document suffix, or None."""
    if not content_type:
        return None
    mime = content_type.split(";", 1)[0].strip().lower()
    for prefix, suffix in _CONTENT_TYPE_SUFFIXES:
        if mime == prefix:
            return suffix
    return None


def is_document_content_type(content_type: Optional[str]) -> bool:
    return content_type_suffix(content_type) is not None


def is_navigation_failure(error_message: Optional[str]) -> bool:
    """True if a browser error looks like "couldn't navigate", not "page
    loaded but something on it failed" - the signature of hitting a file
    download or similar non-HTML response with a real browser."""
    if not error_message:
        return False
    return any(marker in error_message for marker in NAVIGATION_FAILURE_MARKERS)


def resolve_parse_suffix(url: str, content_type: Optional[str]) -> str:
    """The suffix to hand to doc_parse: the URL's own extension if it is a
    known document type, else one inferred from the response content-type."""
    suffix = url_suffix(url)
    if suffix in DOCUMENT_SUFFIXES:
        return suffix
    return content_type_suffix(content_type) or suffix


def parse_document_bytes(url: str, content_type: Optional[str], data: bytes) -> str:
    """Parse document bytes fetched from `url` into markdown/text."""
    from doc_parse import parse_document

    suffix = resolve_parse_suffix(url, content_type)
    return parse_document(f"document{suffix}" if suffix else "document", data)


def filter_document_text(text: str, filter_type: FilterType, query: Optional[str]) -> str:
    """fit/raw/llm return the document's full text; bm25 with a query ranks
    paragraphs the same way BM25ContentFilter ranks HTML chunks."""
    if filter_type != FilterType.BM25 or not query:
        return text
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        return text
    from html import escape

    from crawl4ai.content_filter_strategy import BM25ContentFilter

    html = "<body>" + "".join(f"<p>{escape(p)}</p>" for p in paragraphs) + "</body>"
    chunks = BM25ContentFilter(user_query=query).filter_content(html)
    filtered = "\n\n".join(chunks).strip()
    return filtered or text


class PlainFetchError(Exception):
    """A plain HTTP fallback attempt failed. `.reason` is safe to surface."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _parse_retry_after_seconds(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return 0.0


async def plain_http_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
) -> Tuple[int, httpx.Headers, bytes, str]:
    """GET `url` with a desktop browser UA, honoring one short Retry-After
    on HTTP 429. Returns (status_code, headers, content, final_url).

    The initial URL and every redirect Location are re-validated with
    egress_broker (resolve_and_pin / check_redirect) before being fetched,
    and the request is routed through the pinning proxy
    (egress_broker.get_egress_proxy()) when one is running, so this gets the
    same SSRF guarantees as the browser path's enforce_egress.
    """
    current = url
    retried_429 = False
    proxy = get_egress_proxy()
    async with httpx.AsyncClient(
        verify=True,
        follow_redirects=False,
        timeout=timeout,
        proxy=proxy or None,
    ) as client:
        for _ in range(max_redirects + 1):
            try:
                resolve_and_pin(current)
            except EgressBlocked as exc:
                raise PlainFetchError("URL blocked") from exc

            try:
                response = await client.get(current, headers=DEFAULT_HEADERS)
            except httpx.HTTPError as exc:
                raise PlainFetchError(f"request failed: {exc}") from exc

            if response.status_code == 429 and not retried_429:
                delay = min(
                    _parse_retry_after_seconds(response.headers.get("retry-after")),
                    MAX_RETRY_AFTER_SECONDS,
                )
                if delay > 0:
                    retried_429 = True
                    await asyncio.sleep(delay)
                    continue

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return response.status_code, response.headers, response.content, current
                next_url = urljoin(current, location)
                try:
                    check_redirect(next_url)
                except EgressBlocked as exc:
                    raise PlainFetchError("URL blocked") from exc
                current = next_url
                continue

            return response.status_code, response.headers, response.content, current

    raise PlainFetchError("too many redirects")
