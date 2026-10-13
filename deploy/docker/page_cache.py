"""Shared page cache for every client of this server.

A page read once, by the browser or by a paid backup such as Firecrawl or
Apify, is kept for a while so the next caller does not pay or wait again.
Rows live in one SQLite file on a persistent volume. Expired rows are purged,
and when the stored bytes pass the cap the least recently used rows go first.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

EXTERNAL_PROFILE = "external"
PURGE_TARGET_RATIO = 0.9

_SKIP_PROFILE_KEYS = {"cache_mode", "session_id", "verbose", "stream", "log_console"}
_NO_CACHE_KEYS = ("screenshot", "pdf", "capture_mhtml", "deep_crawl_strategy")


def _flag(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def cache_enabled() -> bool:
    return _flag("CRAWL4AI_PAGE_CACHE", "true").lower() in {"1", "true", "yes"}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def profile_for(params: Dict[str, Any]) -> str:
    """Hash the options that change the page output. Cache switches are ignored."""
    kept = {key: value for key, value in params.items() if key not in _SKIP_PROFILE_KEYS}
    encoded = json.dumps(kept, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def crawl_params(crawler_config: Any) -> Dict[str, Any]:
    if not isinstance(crawler_config, dict):
        return {}
    params = crawler_config.get("params")
    if isinstance(params, dict):
        return params
    return {key: value for key, value in crawler_config.items() if key != "type"}


def cacheable_crawl(params: Dict[str, Any]) -> bool:
    return not any(params.get(key) for key in _NO_CACHE_KEYS)


def cache_mode_of(params: Dict[str, Any]) -> str:
    mode = params.get("cache_mode")
    if isinstance(mode, dict):
        mode = mode.get("params", {}).get("value") or mode.get("value")
    return str(mode or "enabled").strip().lower()


def reads_cache(mode: str) -> bool:
    return mode in {"enabled", "read_only"}


def writes_cache(mode: str) -> bool:
    return mode not in {"disabled", "read_only"}


def markdown_of(result: Dict[str, Any]) -> str:
    markdown = result.get("markdown")
    if isinstance(markdown, str):
        return markdown.strip()
    if isinstance(markdown, dict):
        for key in ("raw_markdown", "markdown_with_citations", "fit_markdown"):
            value = markdown.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def provider_of(result: Dict[str, Any]) -> str:
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        provider = metadata.get("provider")
        if isinstance(provider, str) and provider:
            return provider
    return "crawl4ai"


def external_result(url: str, markdown: str, provider: str, images: Optional[list] = None) -> Dict[str, Any]:
    """Shape a page read by another service like a crawl result."""
    return {
        "url": url,
        "success": True,
        "status_code": 200,
        "error_message": None,
        "html": "",
        "cleaned_html": "",
        "markdown": {
            "raw_markdown": markdown,
            "markdown_with_citations": markdown,
            "references_markdown": "",
            "fit_markdown": None,
            "fit_html": None,
        },
        "media": {"images": images or [], "videos": [], "audios": []},
        "links": {"internal": [], "external": []},
        "metadata": {"provider": provider},
    }


class PageCache:
    def __init__(
        self,
        path: str,
        *,
        ttl_seconds: float,
        max_bytes: int,
        purge_interval_seconds: float = 600,
    ) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self.purge_interval_seconds = purge_interval_seconds
        self._lock = threading.Lock()
        self._last_purge = 0.0
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        if not self._ready:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "url_key TEXT NOT NULL, profile TEXT NOT NULL, url TEXT NOT NULL,"
                "provider TEXT NOT NULL, payload BLOB NOT NULL, size INTEGER NOT NULL,"
                "created_at REAL NOT NULL, expires_at REAL NOT NULL,"
                "last_hit_at REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,"
                "PRIMARY KEY (url_key, profile))"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS pages_expires ON pages(expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS pages_last_hit ON pages(last_hit_at)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            self._ready = True
        return connection

    def _bump(self, connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            "INSERT INTO counters(name, value) VALUES(?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )

    def get(
        self,
        url: str,
        profile: str,
        *,
        any_profile: bool = False,
        external_fallback: bool = True,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        clock = time.time() if now is None else now
        key = normalize_url(url)
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT profile, provider, payload, created_at FROM pages "
                    "WHERE url_key = ? AND profile = ? AND expires_at > ?",
                    (key, profile, clock),
                ).fetchone()
                if row is None and (any_profile or external_fallback):
                    query = (
                        "SELECT profile, provider, payload, created_at FROM pages "
                        "WHERE url_key = ? AND expires_at > ?"
                    )
                    arguments: tuple = (key, clock)
                    if any_profile:
                        query += " AND (profile NOT LIKE 'md:%' OR profile LIKE 'md:raw:%')"
                    else:
                        query += " AND profile = ?"
                        arguments = (key, clock, EXTERNAL_PROFILE)
                    row = connection.execute(query + " ORDER BY created_at DESC LIMIT 1", arguments).fetchone()
                if row is None:
                    self._bump(connection, "misses")
                    connection.commit()
                    return None
                connection.execute(
                    "UPDATE pages SET hits = hits + 1, last_hit_at = ? WHERE url_key = ? AND profile = ?",
                    (clock, key, row[0]),
                )
                self._bump(connection, "hits")
                connection.commit()
            finally:
                connection.close()
        result = json.loads(zlib.decompress(row[2]).decode())
        return {
            "result": result,
            "provider": row[1],
            "profile": row[0],
            "age_seconds": max(0, int(clock - row[3])),
        }

    def put(self, url: str, profile: str, result: Dict[str, Any], provider: str, *, now: Optional[float] = None) -> bool:
        if not markdown_of(result):
            return False
        clock = time.time() if now is None else now
        payload = zlib.compress(json.dumps(result, default=str).encode(), 6)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO pages(url_key, profile, url, provider, payload, size, created_at, expires_at, last_hit_at, hits) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(url_key, profile) DO UPDATE SET url = excluded.url, provider = excluded.provider, "
                    "payload = excluded.payload, size = excluded.size, created_at = excluded.created_at, "
                    "expires_at = excluded.expires_at, last_hit_at = excluded.last_hit_at",
                    (
                        normalize_url(url),
                        profile,
                        url[:2048],
                        provider,
                        payload,
                        len(payload),
                        clock,
                        clock + self.ttl_seconds,
                        clock,
                    ),
                )
                self._bump(connection, f"stored_{provider}")
                connection.commit()
                if clock - self._last_purge >= self.purge_interval_seconds:
                    self._purge(connection, clock)
            finally:
                connection.close()
        return True

    def forget(self, url: str) -> int:
        with self._lock:
            connection = self._connect()
            try:
                removed = connection.execute("DELETE FROM pages WHERE url_key = ?", (normalize_url(url),)).rowcount
                connection.commit()
                return removed
            finally:
                connection.close()

    def purge(self, now: Optional[float] = None) -> Dict[str, int]:
        clock = time.time() if now is None else now
        with self._lock:
            connection = self._connect()
            try:
                return self._purge(connection, clock)
            finally:
                connection.close()

    def _purge(self, connection: sqlite3.Connection, clock: float) -> Dict[str, int]:
        self._last_purge = clock
        expired = connection.execute("DELETE FROM pages WHERE expires_at <= ?", (clock,)).rowcount
        evicted = 0
        total = connection.execute("SELECT COALESCE(SUM(size), 0) FROM pages").fetchone()[0]
        if total > self.max_bytes:
            target = int(self.max_bytes * PURGE_TARGET_RATIO)
            rows = connection.execute("SELECT url_key, profile, size FROM pages ORDER BY last_hit_at ASC").fetchall()
            for url_key, profile, size in rows:
                if total <= target:
                    break
                connection.execute("DELETE FROM pages WHERE url_key = ? AND profile = ?", (url_key, profile))
                total -= size
                evicted += 1
        connection.commit()
        if expired or evicted:
            connection.execute("PRAGMA incremental_vacuum")
            connection.commit()
        return {"expired": expired, "evicted": evicted}

    def stats(self, now: Optional[float] = None) -> Dict[str, Any]:
        clock = time.time() if now is None else now
        with self._lock:
            connection = self._connect()
            try:
                pages, stored_bytes = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM pages WHERE expires_at > ?",
                    (clock,),
                ).fetchone()
                providers = {
                    provider: count
                    for provider, count in connection.execute(
                        "SELECT provider, COUNT(*) FROM pages WHERE expires_at > ? GROUP BY provider",
                        (clock,),
                    ).fetchall()
                }
                counters = dict(connection.execute("SELECT name, value FROM counters").fetchall())
            finally:
                connection.close()
        hits = int(counters.get("hits", 0))
        misses = int(counters.get("misses", 0))
        lookups = hits + misses
        return {
            "enabled": True,
            "pages": pages,
            "stored_mb": round(stored_bytes / 1024 / 1024, 2),
            "max_mb": round(self.max_bytes / 1024 / 1024, 2),
            "ttl_hours": round(self.ttl_seconds / 3600, 2),
            "pages_by_provider": providers,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / lookups, 3) if lookups else None,
            "stored_by_provider": {
                name.removeprefix("stored_"): value for name, value in counters.items() if name.startswith("stored_")
            },
        }


_cache: Optional[PageCache] = None


def get_page_cache() -> Optional[PageCache]:
    global _cache
    if not cache_enabled():
        return None
    if _cache is None:
        ttl_hours = float(_flag("CRAWL4AI_PAGE_CACHE_TTL_HOURS", "24"))
        max_mb = float(_flag("CRAWL4AI_PAGE_CACHE_MAX_MB", "1024"))
        _cache = PageCache(
            _flag("CRAWL4AI_PAGE_CACHE_PATH", "/var/lib/crawl4ai/cache/pages.sqlite"),
            ttl_seconds=ttl_hours * 3600,
            max_bytes=int(max_mb * 1024 * 1024),
            purge_interval_seconds=float(_flag("CRAWL4AI_PAGE_CACHE_PURGE_SECONDS", "600")),
        )
    return _cache


async def cache_get(
    url: str,
    profile: str,
    *,
    any_profile: bool = False,
    external_fallback: bool = True,
) -> Optional[Dict[str, Any]]:
    cache = get_page_cache()
    if cache is None:
        return None
    try:
        return await asyncio.to_thread(
            cache.get,
            url,
            profile,
            any_profile=any_profile,
            external_fallback=external_fallback,
        )
    except Exception:
        return None


async def cache_put(url: str, profile: str, result: Dict[str, Any], provider: str) -> bool:
    cache = get_page_cache()
    if cache is None:
        return False
    try:
        stored = await asyncio.to_thread(cache.put, url, profile, result, provider)
        if stored and provider != "crawl4ai" and profile != EXTERNAL_PROFILE:
            await asyncio.to_thread(cache.put, url, EXTERNAL_PROFILE, result, provider)
        return stored
    except Exception:
        return False
