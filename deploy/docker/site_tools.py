"""Firecrawl-shaped helpers: scrape, map, search, site crawl, agent, download.

Network and browser calls are injected so the tests can run offline.
The live server passes the real crawler and HTTP client.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse
from xml.etree import ElementTree

from doc_parse import html_to_markdown

SEARCH_COOLDOWN_SECONDS = 180
AGENT_PAGE_CAP = 8
DOWNLOAD_PAGE_CAP = 20
SCRAPE_URL_CAP = 10
MAP_SEARCH_POOL = 400

_BLOCK_PHRASES = (
    "captcha",
    "unusual traffic",
    "access denied",
    "attention required",
    "cf-browser-verification",
    "sorry, you have been blocked",
    "enable javascript and cookies",
)
_NONSENSE_SUFFIXES = ("robots.txt", "sitemap.xml", "ads.txt", "favicon.ico")
_SEARCH_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "site", "www",
    "http", "https", "com", "org", "net", "search", "page", "home", "news",
    "web", "linkedin", "profile", "google", "bing",
}
_cooled_until: Dict[str, float] = {}
_search_log = logging.getLogger("crawl4ai.search")


class ToolError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def reset_search_state() -> None:
    _cooled_until.clear()


def cool_engine(name: str, now: float, seconds: float = SEARCH_COOLDOWN_SECONDS) -> None:
    _cooled_until[name] = now + seconds


def engine_cooled(name: str, now: float) -> bool:
    return _cooled_until.get(name, 0) > now


def ensure_http_url(url: str) -> str:
    value = (url or "").strip()
    lowered = value.lower()
    if not lowered.startswith(("http://", "https://")):
        raise ToolError(400, "URL must start with http:// or https://")
    return value


def path_allowed(url: str, include_paths: Sequence[str], exclude_paths: Sequence[str]) -> bool:
    path = urlparse(url).path or "/"
    if not path.startswith("/"):
        path = "/" + path
    for prefix in exclude_paths:
        if prefix and path.startswith(_slash(prefix)):
            return False
    if include_paths:
        return any(path.startswith(_slash(prefix)) for prefix in include_paths if prefix)
    return True


def _slash(prefix: str) -> str:
    prefix = prefix.strip()
    return prefix if prefix.startswith("/") else "/" + prefix


def clamp_crawl(limit: int, max_depth: int, *, max_pages: int = 100, max_depth_cap: int = 5) -> tuple[int, int]:
    if limit < 1:
        raise ToolError(400, "limit must be at least 1")
    if max_depth < 0:
        raise ToolError(400, "max_depth must be at least 0")
    page_cap = max_pages if max_pages else 100
    depth_cap = max_depth_cap if max_depth_cap else 5
    return min(limit, page_cap), min(max_depth, depth_cap)


def narrow_query(query: str, category: Optional[str]) -> str:
    if (category or "").lower() == "developer":
        return f"{query} (site:github.com OR site:readthedocs.io OR site:developer.mozilla.org)"
    return query


def is_blocked_search(status: int, body: str) -> bool:
    if status in {401, 403, 429, 503}:
        return True
    lowered = (body or "").lower()
    return any(phrase in lowered for phrase in _BLOCK_PHRASES)


def query_terms(query: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9]{3,}", query.lower()) if token not in _SEARCH_STOP]


def hit_matches_query(hit: Dict[str, str], terms: Sequence[str]) -> bool:
    hay = " ".join(
        [hit.get("url") or "", hit.get("title") or "", hit.get("description") or ""]
    ).lower()
    distinctive = [token for token in terms if len(token) >= 6]
    if distinctive:
        return any(token in hay for token in distinctive)
    needed = 2 if len(terms) >= 3 else 1
    return sum(1 for token in terms if token in hay) >= needed


def relevant_hits(hits: Sequence[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    terms = query_terms(query)
    if not terms:
        return list(hits)
    return [hit for hit in hits if hit_matches_query(hit, terms)]


def search_hits_are_junk(hits: Sequence[Dict[str, str]], query: str) -> bool:
    """True when the engine answered but the items ignore the query."""
    if not hits:
        return True
    kept = relevant_hits(hits, query)
    if not kept:
        return True
    return len(kept) / len(hits) < 0.4


def merge_hits(groups: Iterable[Sequence[Dict[str, str]]], limit: int) -> List[Dict[str, str]]:
    """Take hits in engine order, drop duplicate URLs, and stop at limit."""
    seen = set()
    merged: List[Dict[str, str]] = []
    for group in groups:
        for hit in group:
            url = hit.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(hit)
            if len(merged) >= limit:
                return merged
    return merged


def seed_source(sitemap: str) -> str:
    mode = (sitemap or "include").lower()
    if mode == "skip":
        return "cc"
    if mode == "only":
        return "sitemap"
    if mode == "include":
        return "sitemap+cc"
    raise ToolError(400, "sitemap must be include, skip, or only")


def parse_sitemap_xml(xml_text: str) -> List[str]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ToolError(400, "Sitemap XML is not readable") from exc
    urls = []
    for node in root.iter():
        if node.tag.endswith("loc") and node.text:
            urls.append(node.text.strip())
    return urls


def is_nonsense_url(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name in _NONSENSE_SUFFIXES


def seed_pattern(url: str) -> str:
    """Keep discovery under the requested path. A bare host stays a whole-site map."""
    parsed = urlparse(url)
    path = (parsed.path or "/").rstrip("/")
    host = (parsed.hostname or "").lower()
    if not path:
        return "*"
    return f"*{host}{path}*"


def filter_mapped(
    items: Sequence[Dict[str, Any]],
    *,
    root: str,
    include_subdomains: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    host = (urlparse(root).hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    prefix = (urlparse(root).path or "/").rstrip("/")
    kept = []
    for item in items:
        url = item.get("url") or ""
        if not url.lower().startswith(("http://", "https://")):
            continue
        if is_nonsense_url(url):
            continue
        parsed = urlparse(url)
        item_host = (parsed.hostname or "").lower()
        item_bare = item_host[4:] if item_host.startswith("www.") else item_host
        if include_subdomains:
            if item_bare != bare and not item_bare.endswith("." + bare):
                continue
        elif item_bare != bare:
            continue
        if prefix:
            item_path = (parsed.path or "/").rstrip("/")
            if item_path != prefix and not item_path.startswith(prefix + "/"):
                continue
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


def rank_mapped(items: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    tokens = [token for token in re.split(r"\W+", query.lower()) if token]
    if not tokens:
        return list(items)

    def score(item: Dict[str, Any]) -> int:
        title = ""
        head = item.get("head_data") or {}
        if isinstance(head, dict):
            title = str(head.get("title") or "")
        haystack = f"{item.get('url', '')} {title}".lower()
        return sum(haystack.count(token) for token in tokens)

    ranked = sorted(items, key=score, reverse=True)
    positive = [item for item in ranked if score(item) > 0]
    return positive or ranked


class _TagFilter(HTMLParser):
    def __init__(self, include: Sequence[str], exclude: Sequence[str]):
        super().__init__(convert_charrefs=True)
        self.include = {tag.lower() for tag in include}
        self.exclude = {tag.lower() for tag in exclude}
        self.skip = 0
        self.stack: List[str] = []
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip or tag in self.exclude:
            self.skip += 1
            return
        if self.include and tag not in self.include and not self.stack:
            return
        self.stack.append(tag)
        attr = {key: value for key, value in attrs}
        self.parts.append(f"<{tag}")
        if attr.get("href"):
            self.parts.append(f' href="{attr["href"]}"')
        self.parts.append(">")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            self.skip -= 1
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.include and not self.stack:
            return
        self.parts.append(data)


def filter_html(html: str, include_tags: Sequence[str], exclude_tags: Sequence[str]) -> str:
    if not include_tags and not exclude_tags:
        return html or ""
    parser = _TagFilter(include_tags, exclude_tags)
    parser.feed(html or "")
    return "".join(parser.parts)


def safe_relpath(url: str, index: int) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part and part not in {".", ".."}]
    if not parts:
        parts = [f"page-{index}"]
    return "/".join(parts)[:180]


def zip_pages(pages: Sequence[Dict[str, Any]], formats: Sequence[str]) -> bytes:
    if len(pages) > DOWNLOAD_PAGE_CAP:
        raise ToolError(400, f"download is capped at {DOWNLOAD_PAGE_CAP} pages")
    chosen = set(formats or ["markdown"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, page in enumerate(pages):
            path = safe_relpath(page.get("url") or "", index)
            if "markdown" in chosen:
                archive.writestr(path + ".md", page.get("markdown") or "")
            if "links" in chosen:
                archive.writestr(path + ".links.txt", "\n".join(page.get("links") or []))
    return buffer.getvalue()


def matches_schema(data: Any, schema: Optional[Dict[str, Any]]) -> bool:
    if not schema:
        return True
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(data, dict):
            return False
        for key in schema.get("required") or []:
            if key not in data:
                return False
        for key, spec in (schema.get("properties") or {}).items():
            if key in data and isinstance(spec, dict) and not matches_schema(data[key], spec):
                return False
        return True
    if expected == "string":
        return isinstance(data, str)
    if expected == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if expected == "integer":
        return isinstance(data, int) and not isinstance(data, bool)
    if expected == "array":
        return isinstance(data, list)
    if expected == "boolean":
        return isinstance(data, bool)
    return True


def extract_json(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


async def run_scrape(
    urls: Sequence[str],
    *,
    formats: Sequence[str],
    only_main_content: bool,
    wait_for: Optional[int],
    include_tags: Sequence[str],
    exclude_tags: Sequence[str],
    query: Optional[str],
    fetch: Callable[..., Awaitable[Dict[str, Any]]],
    complete: Optional[Callable[..., Awaitable[str]]] = None,
) -> Dict[str, Any]:
    if not urls:
        raise ToolError(400, "At least one URL is required")
    if len(urls) > SCRAPE_URL_CAP:
        raise ToolError(400, f"At most {SCRAPE_URL_CAP} URLs")
    pages = []
    for url in urls:
        ensure_http_url(url)
        page = await fetch(
            url,
            only_main=only_main_content,
            wait_for=wait_for,
            include_tags=list(include_tags),
            exclude_tags=list(exclude_tags),
            screenshot="screenshot" in set(formats),
        )
        html = page.get("html") or ""
        if include_tags or exclude_tags:
            html = filter_html(html, include_tags, exclude_tags)
            page["html"] = html
            page["markdown"] = html_to_markdown(html)
        if query:
            if complete is None:
                raise ToolError(500, "NanoGPT is not configured")
            answer = await complete([
                {
                    "role": "user",
                    "content": (
                        "Answer the question using only the page. "
                        "If the page does not say, say you cannot tell.\n\n"
                        f"Question: {query}\n\nPage:\n{(page.get('markdown') or '')[:12000]}"
                    ),
                }
            ])
            pages.append({"url": url, "answer": answer})
            continue
        item = {"url": url, "markdown": page.get("markdown") or ""}
        if "html" in set(formats):
            item["html"] = page.get("html") or ""
        if "links" in set(formats):
            item["links"] = page.get("links") or []
        if "screenshot" in set(formats) and page.get("screenshot_artifact"):
            item["screenshot"] = page["screenshot_artifact"]
        pages.append(item)
    return {"success": True, "data": pages}


async def run_map(
    url: str,
    *,
    limit: int,
    search: Optional[str],
    sitemap: str,
    include_subdomains: bool,
    seeder: Any,
) -> Dict[str, Any]:
    ensure_http_url(url)
    source = seed_source(sitemap)
    config = type("SeedConfig", (), {})()
    config.source = source
    config.pattern = seed_pattern(url)
    config.live_check = False
    config.extract_head = False
    config.concurrency = 10
    config.hits_per_sec = 5
    # A search has to read past the first sitemap page. An earlier short read is
    # cached as if it were complete, so refresh when ranking.
    config.force = bool(search)
    config.verbose = False
    config.max_urls = MAP_SEARCH_POOL if search else limit
    config.query = search
    config.score_threshold = None
    config.scoring_method = "bm25"
    config.filter_nonsense_urls = True
    config.cache_ttl_hours = 24
    config.validate_sitemap_lastmod = True
    domain = urlparse(url).hostname or ""
    found = await seeder.urls(domain, config)
    items = []
    for entry in found or []:
        if isinstance(entry, str):
            items.append({"url": entry})
        elif isinstance(entry, dict) and entry.get("url"):
            items.append(entry)
    if search:
        items = rank_mapped(items, search)
    items = filter_mapped(items, root=url, include_subdomains=include_subdomains, limit=limit)
    return {
        "success": True,
        "links": [item["url"] for item in items],
        "source": source,
    }


def search_request_url(engine: str, query: str, source: str) -> tuple[str, Dict[str, str]]:
    encoded = quote_plus(query)
    news = source == "news"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Crawl4aiSearch/1.0)"}
    if engine == "brave":
        path = "news/search" if news else "web/search"
        return f"https://api.search.brave.com/res/v1/{path}?q={encoded}&count=20", headers
    if engine == "duckduckgo":
        return f"https://html.duckduckgo.com/html/?q={encoded}", headers
    if engine == "bing":
        host = "https://www.bing.com/news/search" if news else "https://www.bing.com/search"
        return f"{host}?{urlencode({'q': query, 'format': 'rss'})}", headers
    if engine == "mojeek":
        return f"https://www.mojeek.com/search?q={encoded}", headers
    if engine == "searxng":
        base = "https://searx.tiekoetter.com/search"
        category = "&categories=news" if news else ""
        return f"{base}?q={encoded}&format=json{category}", headers
    if engine == "apify":
        return (
            "https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?timeout=120&memory=512",
            {},
        )
    raise ToolError(400, f"Unknown search engine {engine}")


def parse_search_body(engine: str, body: str) -> List[Dict[str, str]]:
    if engine == "brave":
        return _parse_brave(body)
    if engine == "duckduckgo":
        return _parse_ddg(body)
    if engine == "bing":
        return _parse_rss(body, engine)
    if engine == "mojeek":
        return _parse_mojeek(body)
    if engine == "searxng":
        return _parse_searx(body)
    if engine == "apify":
        return _parse_apify(body)
    return []


def _parse_brave(body: str) -> List[Dict[str, str]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows = (payload.get("web") or {}).get("results") or payload.get("results") or []
    hits = []
    for row in rows:
        url = row.get("url") or ""
        if url:
            hits.append({
                "url": url,
                "title": row.get("title") or "",
                "description": row.get("description") or "",
                "engine": "brave",
            })
    return hits


def _parse_ddg(body: str) -> List[Dict[str, str]]:
    hits = []
    for match in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        body,
        flags=re.I | re.S,
    ):
        url = _ddg_target(match.group(1))
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if url:
            hits.append({"url": url, "title": title, "description": "", "engine": "duckduckgo"})
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)>', body, flags=re.I | re.S)
    for index, snippet in enumerate(snippets):
        if index < len(hits):
            hits[index]["description"] = re.sub(r"<[^>]+>", "", snippet).strip()
    return hits


def _ddg_target(href: str) -> str:
    if "uddg=" in href:
        parsed = urlparse(href)
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


def _parse_rss(body: str, engine: str) -> List[Dict[str, str]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    hits = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        description = re.sub(r"<[^>]+>", "", item.findtext("description") or "").strip()
        if url:
            hits.append({"url": url, "title": title, "description": description, "engine": engine})
    return hits


def _parse_mojeek(body: str) -> List[Dict[str, str]]:
    hits = []
    for match in re.finditer(r'<a class="ob" href="(https?://[^"]+)"[^>]*>(.*?)</a>', body, flags=re.I | re.S):
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        hits.append({"url": match.group(1), "title": title, "description": "", "engine": "mojeek"})
    return hits


def _parse_searx(body: str) -> List[Dict[str, str]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    hits = []
    for row in payload.get("results") or []:
        url = row.get("url") or ""
        if url:
            hits.append({
                "url": url,
                "title": row.get("title") or "",
                "description": row.get("content") or "",
                "engine": "searxng",
            })
    return hits


def apify_input(query: str, source: str) -> Dict[str, Any]:
    """One Google results page. Paid add-ons stay off."""
    term = query
    if source == "news":
        term = "https://www.google.com/search?tbm=nws&q=" + quote_plus(query)
    return {
        "queries": term,
        "maxPagesPerQuery": 1,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": False,
        "focusOnPaidAds": False,
        "maximumLeadsEnrichmentRecords": 0,
        "aiOverview": {"scrapeFullAiOverview": False},
        "aiModeSearch": {"enableAiMode": False},
        "perplexitySearch": {"enablePerplexity": False},
        "chatGptSearch": {"enableChatGpt": False},
        "copilotSearch": {"enableCopilot": False},
        "geminiSearch": {"enableGemini": False},
        "websiteContentScraper": {"enable": False},
    }


def _parse_apify(body: str) -> List[Dict[str, str]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else payload.get("items") or []
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for item in row.get("organicResults") or []:
            url = item.get("url") or ""
            if not url:
                continue
            hits.append({
                "url": url,
                "title": item.get("title") or "",
                "description": item.get("description") or "",
                "engine": "apify",
            })
    return hits


def engine_names(apify_token: str = "") -> List[str]:
    names = ["duckduckgo", "bing", "mojeek", "searxng"]
    if apify_token:
        names.append("apify")
    return names


async def run_search(
    query: str,
    *,
    limit: int,
    sources: str,
    categories: Optional[str],
    apify_token: str = "",
    fetch: Callable[..., Awaitable[tuple[int, str]]],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    if not query.strip():
        raise ToolError(400, "query is required")
    if limit < 1:
        raise ToolError(400, "limit must be at least 1")
    source = "news" if sources == "news" else "web"
    narrowed = narrow_query(query.strip(), categories)
    clock = time.time() if now is None else now
    groups = []
    used = []
    skipped = []
    for name in engine_names(apify_token):
        if engine_cooled(name, clock):
            skipped.append({"engine": name, "reason": "cooldown"})
            continue
        url, headers = search_request_url(name, narrowed, source)
        payload = None
        try:
            if name == "apify":
                headers = {"Authorization": f"Bearer {apify_token}"}
                payload = apify_input(narrowed, source)
                status, body = await fetch(name, url, headers, payload)
            else:
                status, body = await fetch(name, url, headers)
        except Exception:
            _search_log.warning("search engine %s failed for %r", name, narrowed, exc_info=True)
            skipped.append({"engine": name, "reason": "error"})
            continue
        if is_blocked_search(status, body):
            cool_engine(name, clock)
            skipped.append({"engine": name, "reason": "blocked"})
            continue
        if status >= 400:
            skipped.append({"engine": name, "reason": "error"})
            continue
        hits = parse_search_body(name, body)
        if name != "apify" and search_hits_are_junk(hits, narrowed):
            _search_log.warning(
                "search engine %s returned irrelevant hits for %r", name, narrowed
            )
            skipped.append({"engine": name, "reason": "irrelevant"})
            continue
        kept = hits if name == "apify" else relevant_hits(hits, narrowed)
        used.append(name)
        groups.append(kept)
        if len(merge_hits(groups, limit)) >= limit:
            break
    hits = merge_hits(groups, limit)
    bucket = "news" if source == "news" else "web"
    return {
        "success": True,
        "data": {bucket: hits},
        "engines_used": used,
        "engines_skipped": skipped,
        "query": narrowed,
    }


async def run_agent(
    prompt: str,
    urls: Sequence[str],
    schema: Optional[Dict[str, Any]],
    *,
    scrape: Callable[[str], Awaitable[Dict[str, Any]]],
    complete: Callable[[List[Dict[str, str]]], Awaitable[str]],
    discover: Optional[Callable[[str], Awaitable[Sequence[str]]]] = None,
    max_pages: int = AGENT_PAGE_CAP,
) -> Dict[str, Any]:
    if not prompt.strip():
        raise ToolError(400, "prompt is required")
    if urls:
        chosen = [ensure_http_url(url) for url in list(urls)[:max_pages]]
    else:
        if discover is None:
            raise ToolError(400, "Provide urls or allow discovery")
        chosen = [ensure_http_url(url) for url in list(await discover(prompt))[:max_pages]]
    pages = []
    for url in chosen:
        page = await scrape(url)
        pages.append({"url": url, "markdown": (page.get("markdown") or "")[:8000]})
    page_blob = "\n\n".join(f"URL: {page['url']}\n{page['markdown']}" for page in pages)
    instruction = (
        "Extract the answer from the pages. Do not invent facts that are not in the pages.\n"
        f"Task: {prompt}\n\n{page_blob}"
    )
    if schema:
        instruction += "\n\nReturn one JSON object matching this schema:\n" + json.dumps(schema)
    raw = await complete([{"role": "user", "content": instruction}])
    if schema:
        try:
            data = extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ToolError(502, "The model did not return JSON") from exc
        if not matches_schema(data, schema):
            raise ToolError(502, "The model JSON did not match the schema")
    else:
        data = {"answer": raw}
    return {
        "success": True,
        "data": data,
        "sources": [page["url"] for page in pages],
        "pages_fetched": len(pages),
    }


def strip_tracking(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = []
    for key, value in parse_qs(parsed.query, keep_blank_values=True).items():
        if key.startswith("utm_") or key in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}:
            continue
        for item in value:
            kept.append((key, item))
    query = urlencode(kept)
    return urlunparse(parsed._replace(query=query))


def normalize_snapshot(text: str) -> str:
    def _clean(match: re.Match) -> str:
        return strip_tracking(match.group(0).rstrip(".,)"))

    cleaned = re.sub(r"https?://\S+", _clean, text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned
