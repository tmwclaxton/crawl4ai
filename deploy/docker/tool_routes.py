"""HTTP routes for the Firecrawl-shaped MCP tools.

Registered from server.py before the MCP bridge scans the app, so each
handler is exposed as a tool.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from artifacts import write_artifact
from mcp_bridge import mcp_tool
from nanogpt_client import NanoGPTError, complete
from page_monitor import PageMonitor, nanogpt_judge, webhook_static_error
from schemas import (
    AgentRequest,
    DownloadRequest,
    MapRequest,
    MonitorCreateRequest,
    MonitorIdRequest,
    ParseRequest,
    ScrapeRequest,
    SearchRequest,
    SiteCrawlRequest,
)
from site_tools import (
    ToolError,
    path_allowed,
    run_agent,
    run_map,
    run_scrape,
    run_search,
    zip_pages,
)
from utils import validate_url_destination, validate_webhook_url


def _raise_tool(exc: ToolError) -> None:
    raise HTTPException(exc.status, exc.detail)


async def _complete(messages: List[Dict[str, str]]) -> str:
    try:
        return await complete(messages)
    except NanoGPTError as exc:
        status = 502 if exc.status in {401, 403} else exc.status
        raise ToolError(status, exc.detail) from exc


def _markdown_text(result: Any, only_main: bool) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    if isinstance(markdown, str):
        return markdown
    if only_main:
        return getattr(markdown, "fit_markdown", None) or getattr(markdown, "raw_markdown", None) or ""
    return getattr(markdown, "raw_markdown", None) or getattr(markdown, "fit_markdown", None) or ""


def _links(result: Any) -> List[str]:
    raw = getattr(result, "links", None) or {}
    found: List[str] = []
    groups = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    for group in groups:
        if isinstance(group, str):
            found.append(group)
            continue
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict) and item.get("href"):
                found.append(item["href"])
    return found


async def _browser_page(
    url: str,
    *,
    browser_config_factory: Callable[[], Any],
    only_main: bool,
    wait_for: Optional[int],
    include_tags: List[str],
    exclude_tags: List[str],
    screenshot: bool,
) -> Dict[str, Any]:
    from crawl4ai import CrawlerRunConfig
    from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    from crawler_pool import get_crawler, release_crawler

    try:
        validate_url_destination(url)
    except HTTPException as exc:
        raise ToolError(exc.status_code, str(exc.detail)) from exc

    excluded = list(exclude_tags)
    if only_main:
        for tag in ("nav", "footer"):
            if tag not in excluded:
                excluded.append(tag)
    crawler = None
    try:
        config = CrawlerRunConfig(
            markdown_generator=DefaultMarkdownGenerator(),
            scraping_strategy=LXMLWebScrapingStrategy(),
            excluded_tags=excluded,
            target_elements=include_tags or None,
            screenshot=screenshot,
            delay_before_return_html=(wait_for or 0) / 1000 if wait_for else 0.1,
        )
        crawler = await get_crawler(browser_config_factory())
        result = await crawler.arun(url=url, config=config)
        if isinstance(result, list):
            result = result[0] if result else None
        if result is None or not getattr(result, "success", False):
            status_code = getattr(result, "status_code", None) if result is not None else None
            if status_code == 404:
                return {"url": url, "removed": True, "markdown": "", "html": "", "links": []}
            detail = getattr(result, "error_message", None) if result is not None else "Crawl failed"
            raise ToolError(502, detail or "Crawl failed")
        page: Dict[str, Any] = {
            "url": url,
            "markdown": _markdown_text(result, only_main),
            "html": getattr(result, "html", None) or "",
            "links": _links(result),
        }
        if screenshot and getattr(result, "screenshot", None):
            meta = write_artifact("png", base64.b64decode(result.screenshot))
            page["screenshot_artifact"] = {
                "artifact_id": meta["artifact_id"],
                "url": f"/artifacts/{meta['artifact_id']}",
            }
        return page
    except ToolError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        raise ToolError(502, str(exc)) from exc
    finally:
        if crawler is not None:
            await release_crawler(crawler)


async def _site_crawl(
    url: str,
    *,
    limit: int,
    max_depth: int,
    include_paths: List[str],
    exclude_paths: List[str],
    browser_config_factory: Callable[[], Any],
    max_pages: int,
    max_depth_cap: int,
) -> Dict[str, Any]:
    from crawl4ai import CrawlerRunConfig
    from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
    from crawl4ai.deep_crawling.filters import URLFilter, FilterChain
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    from crawler_pool import get_crawler, release_crawler
    from site_tools import clamp_crawl, ensure_http_url

    root = ensure_http_url(url)
    page_limit, depth = clamp_crawl(limit, max_depth, max_pages=max_pages, max_depth_cap=max_depth_cap)

    class PathRuleFilter(URLFilter):
        __slots__ = ("include", "exclude")

        def __init__(self, include, exclude):
            super().__init__(name="path-rule")
            self.include = tuple(include)
            self.exclude = tuple(exclude)

        def apply(self, candidate: str) -> bool:
            allowed = path_allowed(candidate, self.include, self.exclude)
            self._update_stats(allowed)
            return allowed

    filters = []
    if include_paths or exclude_paths:
        filters.append(PathRuleFilter(include_paths, exclude_paths))
    strategy = BFSDeepCrawlStrategy(
        max_depth=depth,
        max_pages=page_limit,
        filter_chain=FilterChain(filters),
        include_external=False,
    )
    crawler = None
    try:
        config = CrawlerRunConfig(
            deep_crawl_strategy=strategy,
            markdown_generator=DefaultMarkdownGenerator(),
            scraping_strategy=LXMLWebScrapingStrategy(),
            excluded_tags=["nav", "footer"],
        )
        crawler = await get_crawler(browser_config_factory())
        results = await crawler.arun(url=root, config=config)
        if not isinstance(results, list):
            results = [results]
        pages = []
        for result in results:
            if not getattr(result, "success", False):
                continue
            page_url = getattr(result, "url", root)
            if page_url != root and not path_allowed(page_url, include_paths, exclude_paths):
                continue
            pages.append({
                "url": page_url,
                "markdown": _markdown_text(result, True),
            })
            if len(pages) >= page_limit:
                break
        return {"success": True, "data": pages, "limit": page_limit, "max_depth": depth}
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(502, str(exc)) from exc
    finally:
        if crawler is not None:
            await release_crawler(crawler)


def attach_tool_routes(app, limiter, token_dep, config, browser_config_factory, redis) -> None:
    limit = config["rate_limiting"]["default_limit"]
    caps = config.get("limits") or {}

    async def fetch_page(url: str, **kwargs) -> Dict[str, Any]:
        return await _browser_page(url, browser_config_factory=browser_config_factory, **kwargs)

    async def fetch_text(url: str) -> Optional[str]:
        page = await fetch_page(
            url,
            only_main=True,
            wait_for=None,
            include_tags=[],
            exclude_tags=[],
            screenshot=False,
        )
        if page.get("removed"):
            return None
        return page.get("markdown") or ""

    app.state.page_monitor = PageMonitor(redis, fetch=fetch_text, judge=nanogpt_judge)

    @app.post("/scrape")
    @limiter.limit(limit)
    @mcp_tool("scrape")
    async def scrape(request: Request, body: ScrapeRequest, _td: Dict = Depends(token_dep)):
        """Read one or more public pages as markdown. Pass query to ask NanoGPT about the page instead of returning it."""
        try:
            return await run_scrape(
                body.urls,
                formats=body.formats,
                only_main_content=body.only_main_content,
                wait_for=body.wait_for,
                include_tags=body.include_tags,
                exclude_tags=body.exclude_tags,
                query=body.query,
                fetch=fetch_page,
                complete=_complete,
            )
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/map")
    @limiter.limit(limit)
    @mcp_tool("map")
    async def map_site(request: Request, body: MapRequest, _td: Dict = Depends(token_dep)):
        """List URLs on a site from its sitemap and Common Crawl. Pass search to rank them."""
        from crawl4ai import AsyncUrlSeeder

        try:
            async with AsyncUrlSeeder() as seeder:
                return await run_map(
                    body.url,
                    limit=body.limit,
                    search=body.search,
                    sitemap=body.sitemap,
                    include_subdomains=body.include_subdomains,
                    seeder=seeder,
                )
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/search")
    @limiter.limit(limit)
    @mcp_tool("search")
    async def search(request: Request, body: SearchRequest, _td: Dict = Depends(token_dep)):
        """Search the web across several engines and skip any engine that blocks the request."""
        import httpx

        brave_key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY") or ""

        async def fetch(name: str, url: str, headers: Dict[str, str]):
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)
                return response.status_code, response.text

        try:
            result = await run_search(
                body.query,
                limit=body.limit,
                sources=body.sources,
                categories=body.categories,
                brave_key=brave_key,
                fetch=fetch,
            )
            if body.scrape:
                hits = result["data"].get("web") or result["data"].get("news") or []
                for hit in hits:
                    try:
                        page = await fetch_page(
                            hit["url"],
                            only_main=True,
                            wait_for=None,
                            include_tags=[],
                            exclude_tags=[],
                            screenshot=False,
                        )
                        hit["markdown"] = page.get("markdown") or ""
                    except ToolError as exc:
                        hit["markdown_error"] = exc.detail
            return result
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/site-crawl")
    @limiter.limit(limit)
    @mcp_tool("site_crawl")
    async def site_crawl(request: Request, body: SiteCrawlRequest, _td: Dict = Depends(token_dep)):
        """Crawl one site section with a page limit, a depth, and include or exclude paths."""
        try:
            return await _site_crawl(
                body.url,
                limit=body.limit,
                max_depth=body.max_depth,
                include_paths=body.include_paths,
                exclude_paths=body.exclude_paths,
                browser_config_factory=browser_config_factory,
                max_pages=int(caps.get("max_pages") or 100),
                max_depth_cap=int(caps.get("max_depth") or 5),
            )
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/agent")
    @limiter.limit(limit)
    @mcp_tool("agent")
    async def agent(request: Request, body: AgentRequest, _td: Dict = Depends(token_dep)):
        """Extract structured data from at most 8 pages using NanoGPT. Does not click or log in."""
        async def scrape_one(url: str) -> Dict[str, Any]:
            return await fetch_page(
                url,
                only_main=True,
                wait_for=None,
                include_tags=[],
                exclude_tags=[],
                screenshot=False,
            )

        async def discover(prompt: str) -> List[str]:
            import httpx

            brave_key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY") or ""

            async def fetch(name: str, target: str, headers: Dict[str, str]):
                async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                    response = await client.get(target, headers=headers)
                    return response.status_code, response.text

            found = await run_search(
                prompt,
                limit=5,
                sources="web",
                categories=None,
                brave_key=brave_key,
                fetch=fetch,
            )
            return [hit["url"] for hit in found["data"].get("web") or []]

        try:
            urls = list(body.urls)
            return await run_agent(
                body.prompt,
                urls,
                body.schema_,
                scrape=scrape_one,
                complete=_complete,
                discover=None if urls else discover,
            )
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/parse")
    @limiter.limit(limit)
    @mcp_tool("parse")
    async def parse_file(request: Request, body: ParseRequest, _td: Dict = Depends(token_dep)):
        """Turn a PDF, DOCX, XLSX, HTML, ODT, or RTF file into markdown. summary and query use NanoGPT."""
        from doc_parse import ParseError, parse_document

        try:
            data = base64.b64decode(body.data_base64, validate=True)
        except Exception as exc:
            raise HTTPException(400, "data_base64 is not valid base64") from exc
        try:
            markdown = parse_document(body.filename, data)
        except ParseError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        if body.summary or body.query:
            question = body.query or "Summarize this document in a short paragraph."
            try:
                answer = await _complete([
                    {"role": "user", "content": f"{question}\n\nDocument:\n{markdown[:12000]}"}
                ])
            except ToolError as exc:
                _raise_tool(exc)
            if body.query and not body.summary:
                return {"success": True, "answer": answer, "filename": body.filename}
            return {"success": True, "markdown": markdown, "summary": answer, "filename": body.filename}
        return {"success": True, "markdown": markdown, "filename": body.filename}

    @app.post("/download")
    @limiter.limit(limit)
    @mcp_tool("download")
    async def download(request: Request, body: DownloadRequest, _td: Dict = Depends(token_dep)):
        """Save a small site section as a zip of markdown files in the artifact store."""
        from crawl4ai import AsyncUrlSeeder

        try:
            async with AsyncUrlSeeder() as seeder:
                mapped = await run_map(
                    body.url,
                    limit=body.limit,
                    search=body.search,
                    sitemap="include",
                    include_subdomains=False,
                    seeder=seeder,
                )
            links = [link for link in mapped["links"] if path_allowed(link, body.include_paths, body.exclude_paths)]
            if body.url not in links:
                links.insert(0, body.url)
            links = links[: body.limit]
            pages = []
            for link in links:
                page = await fetch_page(
                    link,
                    only_main=body.only_main_content,
                    wait_for=None,
                    include_tags=[],
                    exclude_tags=[],
                    screenshot="screenshot" in set(body.formats),
                )
                pages.append(page)
            blob = zip_pages(pages, body.formats)
            meta = write_artifact("zip", blob)
            return {
                "success": True,
                "pages": len(pages),
                "artifact_id": meta["artifact_id"],
                "url": f"/artifacts/{meta['artifact_id']}",
            }
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/monitor/create")
    @limiter.limit(limit)
    @mcp_tool("monitor_create")
    async def monitor_create(request: Request, body: MonitorCreateRequest, _td: Dict = Depends(token_dep)):
        """Watch pages on a schedule. The webhook must be public https. Minimum schedule is 15 minutes."""
        if body.webhook:
            problem = webhook_static_error(body.webhook)
            if problem:
                raise HTTPException(400, problem)
            try:
                validate_webhook_url(body.webhook)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        try:
            return await app.state.page_monitor.create(
                name=body.name,
                urls=body.urls,
                schedule=body.schedule,
                goal=body.goal,
                webhook=body.webhook,
            )
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/monitor/list")
    @limiter.limit(limit)
    @mcp_tool("monitor_list")
    async def monitor_list(request: Request, _td: Dict = Depends(token_dep)):
        """List page watches."""
        return await app.state.page_monitor.list_monitors()

    @app.post("/monitor/check")
    @limiter.limit(limit)
    @mcp_tool("monitor_check")
    async def monitor_check(request: Request, body: MonitorIdRequest, _td: Dict = Depends(token_dep)):
        """Check a watch now. Pages come back as same, new, changed, removed, or error."""
        try:
            return await app.state.page_monitor.check(body.id)
        except ToolError as exc:
            _raise_tool(exc)

    @app.post("/monitor/delete")
    @limiter.limit(limit)
    @mcp_tool("monitor_delete")
    async def monitor_delete(request: Request, body: MonitorIdRequest, _td: Dict = Depends(token_dep)):
        """Delete a page watch."""
        try:
            return await app.state.page_monitor.delete(body.id)
        except ToolError as exc:
            _raise_tool(exc)
