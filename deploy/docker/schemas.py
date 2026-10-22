from typing import Any, List, Optional, Dict
from enum import Enum
from pydantic import BaseModel, Field, HttpUrl, field_validator
from utils import FilterType


class CrawlRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=100)
    browser_config: Optional[Dict] = Field(default_factory=dict)
    crawler_config: Optional[Dict] = Field(default_factory=dict)
    crawler_configs: Optional[List[Dict]] = Field(
        default=None,
        description=(
            "List of per-URL CrawlerRunConfig dicts for arun_many(). "
            "When provided, each config can include a 'url_matcher' pattern "
            "to match against specific URLs. Takes precedence over crawler_config."
        ),
    )
    site_profile: Optional[str] = Field(
        default=None,
        description=(
            "Opt-in site profile. 'youtube' adds YouTube consent cookies "
            "(SOCS/CONSENT) and an English Accept-Language header so channel "
            "pages render instead of the EU consent wall. youtube.com URLs only. "
            "Omit for the default behaviour."
        ),
    )


class HookSpec(BaseModel):
    """A single declarative hook: a fixed action plus schema-validated params.

    Arbitrary Python (the old `code` map) is no longer accepted - it was an
    exec()-based RCE surface. Available actions are enumerated by GET /hooks/info
    and validated server-side by hook_registry.py.
    """
    action: str = Field(..., description="One of the registered hook actions")
    params: Dict[str, Any] = Field(default_factory=dict, description="Action parameters")


class HookConfig(BaseModel):
    """Configuration for declarative hooks."""
    hooks: List[HookSpec] = Field(
        default_factory=list,
        max_length=10,
        description="Declarative hook specs (action + params), max 10",
    )
    timeout: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Timeout in seconds for each hook execution",
    )
    # Legacy 0.8.x field: inline-Python hook code, removed in 0.9.0 (it was an
    # exec()-based RCE surface). Captured here (instead of being dropped by
    # pydantic) solely so the server can tell the caller it was NOT executed;
    # it is never run.
    code: Optional[Any] = Field(
        default=None,
        description="REMOVED in 0.9.0: inline hook code is accepted for compatibility but never executed",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "hooks": [
                    {"action": "block_resources", "params": {"resource_types": ["image", "font"]}},
                    {"action": "scroll_to_bottom", "params": {"max_steps": 10, "delay_ms": 500}},
                ],
                "timeout": 30,
            }
        }


class CrawlRequestWithHooks(CrawlRequest):
    """Extended crawl request with hooks support"""
    hooks: Optional[HookConfig] = Field(
        default=None,
        description="Optional user-provided hook functions"
    )

class MarkdownRequest(BaseModel):
    """Request body for the /md endpoint."""
    url: str                    = Field(...,  description="Absolute http/https URL to fetch")
    f:   FilterType             = Field(FilterType.FIT, description="Content‑filter strategy: fit, raw, bm25, or llm")
    q:   Optional[str] = Field(None,  description="Query string used by BM25/LLM filters")
    c:   Optional[str] = Field("0",   description="Cache‑bust / revision counter")
    provider: Optional[str] = Field(None, description="LLM provider override (e.g., 'anthropic/claude-3-opus')")
    temperature: Optional[float] = Field(None, description="LLM temperature override (0.0-2.0)")
    # base_url removed: a request-supplied LLM endpoint was a credential-exfil
    # vector. The endpoint is derived server-side from the provider name.


class RawCode(BaseModel):
    code: str

class HTMLRequest(BaseModel):
    url: str
    
class ScreenshotRequest(BaseModel):
    url: str
    screenshot_wait_for: Optional[float] = 2
    wait_for_images: Optional[bool] = False
    # Deprecated no-op: caller paths were an arbitrary-write -> RCE vector.
    # Never written; the server stores results in the artifact store instead.
    output_path: Optional[str] = Field(
        default=None,
        deprecated=True,
        description=(
            "REMOVED in 0.9.0 and ignored - no file is written. Results are "
            "stored server-side; fetch via GET /artifacts/{artifact_id}."
        ),
    )


class PDFRequest(BaseModel):
    url: str
    # output_path deprecated no-op (see ScreenshotRequest).
    output_path: Optional[str] = Field(
        default=None,
        deprecated=True,
        description=(
            "REMOVED in 0.9.0 and ignored - no file is written. Results are "
            "stored server-side; fetch via GET /artifacts/{artifact_id}."
        ),
    )


class ScrapeRequest(BaseModel):
    """One or more public pages, in the same shape agents expect from a scrape tool."""
    urls: List[str] = Field(..., min_length=1, max_length=10)
    formats: List[str] = Field(default_factory=lambda: ["markdown"])
    only_main_content: bool = True
    wait_for: Optional[int] = Field(None, ge=0, le=30000, description="Milliseconds to wait after load")
    include_tags: List[str] = Field(default_factory=list)
    exclude_tags: List[str] = Field(default_factory=list)
    query: Optional[str] = None


class CacheStoreRequest(BaseModel):
    """A page read by another service, kept so later callers reuse it."""
    url: str = Field(..., max_length=2048)
    markdown: str = Field(..., min_length=1, max_length=5_000_000)
    provider: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    images: List[Dict[str, Any]] = Field(default_factory=list, max_length=200)


class CacheForgetRequest(BaseModel):
    url: str = Field(..., max_length=2048)


class MapRequest(BaseModel):
    """Discover URLs on a site from its sitemap and, optionally, Common Crawl."""
    url: str
    limit: int = Field(50, ge=1, le=200)
    search: Optional[str] = None
    sitemap: str = Field("include", description="include, skip, or only")
    include_subdomains: bool = False


class SearchRequest(BaseModel):
    """Web search merged from several engines. A blocked engine is skipped."""
    query: str = Field(..., min_length=1, max_length=400)
    limit: int = Field(5, ge=1, le=20)
    sources: str = Field("web", description="web or news")
    categories: Optional[str] = None
    scrape: bool = False


class SiteCrawlRequest(BaseModel):
    """Crawl one site section. This is the friendly crawl, not the raw /crawl config."""
    url: str
    limit: int = Field(10, ge=1, le=100)
    max_depth: int = Field(2, ge=0, le=5)
    include_paths: List[str] = Field(default_factory=list)
    exclude_paths: List[str] = Field(default_factory=list)


class AgentRequest(BaseModel):
    """Extract structured data from a few pages. Does not click or log in."""
    prompt: str = Field(..., min_length=1, max_length=4000)
    urls: List[str] = Field(default_factory=list)
    schema_: Optional[Dict[str, Any]] = Field(None, alias="schema")

    model_config = {"populate_by_name": True}


class ParseRequest(BaseModel):
    """Turn an uploaded document into markdown. The file is base64."""
    filename: str
    data_base64: str
    summary: bool = False
    query: Optional[str] = None


class DownloadRequest(BaseModel):
    """Map a site and save a zip of page files in the artifact store."""
    url: str
    limit: int = Field(5, ge=1, le=20)
    search: Optional[str] = None
    include_paths: List[str] = Field(default_factory=list)
    exclude_paths: List[str] = Field(default_factory=list)
    formats: List[str] = Field(default_factory=lambda: ["markdown"])
    only_main_content: bool = True


class MonitorCreateRequest(BaseModel):
    """Watch pages and report when the content actually changes."""
    name: str = Field(..., min_length=1, max_length=120)
    urls: List[str] = Field(..., min_length=1, max_length=20)
    schedule: str = "hourly"
    goal: Optional[str] = None
    webhook: Optional[str] = None


class MonitorIdRequest(BaseModel):
    id: str


class JSEndpointRequest(BaseModel):
    url: str
    scripts: List[str] = Field(
        ...,
        description="List of separated JavaScript snippets to execute"
    )


class WebhookConfig(BaseModel):
    """Configuration for webhook notifications."""
    webhook_url: HttpUrl
    webhook_data_in_payload: bool = False
    webhook_headers: Optional[Dict[str, str]] = None

    @field_validator("webhook_headers")
    @classmethod
    def _validate_headers(cls, v):
        # Reject unsafe outbound headers early (422). Mirrors
        # webhook.sanitize_webhook_headers; kept inline to avoid an import cycle.
        if not v:
            return v
        from webhook import sanitize_webhook_headers
        return sanitize_webhook_headers(v)


class WebhookPayload(BaseModel):
    """Payload sent to webhook endpoints."""
    task_id: str
    task_type: str  # "crawl", "llm_extraction", etc.
    status: str  # "completed" or "failed"
    timestamp: str  # ISO 8601 format
    urls: List[str]
    error: Optional[str] = None
    data: Optional[Dict] = None  # Included only if webhook_data_in_payload=True