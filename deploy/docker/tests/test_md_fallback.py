"""Offline tests for the /md fallback chain (md_fallback.py + api.py).

No real network, browser, or DNS: httpx.AsyncClient, the crawler, and
egress_broker's resolution are all mocked or driven with IP-literal URLs so
getaddrinfo never does a real lookup.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import api
import doc_parse
import md_fallback
from utils import FilterType, load_config

_CONFIG = load_config()


# ─────────────────────────── helpers ───────────────────────────

class _FakeResponse:
    def __init__(self, status_code, headers=None, content=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


class _FakeAsyncClient:
    """Drop-in for httpx.AsyncClient. `responses` is a list consumed in
    order by successive .get() calls; the last entry repeats if exhausted."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _install_fake_client(monkeypatch, responses, calls=None):
    calls = calls if calls is not None else []

    class Client(_FakeAsyncClient):
        async def get(self, url, headers=None):
            calls.append(url)
            index = min(len(calls) - 1, len(responses) - 1)
            return responses[index]

    monkeypatch.setattr(md_fallback.httpx, "AsyncClient", Client)
    return calls


def _clear_dataimpulse(monkeypatch):
    for name in (
        "CRAWL4AI_PROXY_FALLBACK",
        "DATAIMPULSE_HOST", "DATAIMPULSE_PORT",
        "DATAIMPULSE_DATACENTER_USERNAME", "DATAIMPULSE_DATACENTER_PASSWORD",
        "DATAIMPULSE_RESIDENTIAL_USERNAME", "DATAIMPULSE_RESIDENTIAL_PASSWORD",
        "DATAIMPULSE_PREMIUM_USERNAME", "DATAIMPULSE_PREMIUM_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_tiers(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_HOST", "gw.dataimpulse.com")
    monkeypatch.setenv("DATAIMPULSE_PORT", "823")
    monkeypatch.setenv("DATAIMPULSE_DATACENTER_USERNAME", "dc-user")
    monkeypatch.setenv("DATAIMPULSE_DATACENTER_PASSWORD", "dc-pass")
    monkeypatch.setenv("DATAIMPULSE_RESIDENTIAL_USERNAME", "res-user")
    monkeypatch.setenv("DATAIMPULSE_RESIDENTIAL_PASSWORD", "res-pass")
    monkeypatch.setenv("DATAIMPULSE_PREMIUM_USERNAME", "prem-user")
    monkeypatch.setenv("DATAIMPULSE_PREMIUM_PASSWORD", "prem-pass")


class _FailedCrawler:
    """arun() always returns the same browser failure."""

    def __init__(self, error_message="Blocked by anti-bot protection: HTTP 429"):
        self.error_message = error_message
        self.calls = []

    async def arun(self, url, config=None, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(success=False, error_message=self.error_message)


def _patch_crawler_pool(monkeypatch, crawler):
    import crawler_pool

    async def get_crawler(*args, **kwargs):
        return crawler

    async def release_crawler(*args, **kwargs):
        return None

    monkeypatch.setattr(crawler_pool, "get_crawler", get_crawler)
    monkeypatch.setattr(crawler_pool, "release_crawler", release_crawler)


@pytest.fixture(autouse=True)
def _offline_defaults(monkeypatch):
    # No DataImpulse credentials unless a test opts in.
    _clear_dataimpulse(monkeypatch)
    # Egress pinning proxy isn't running in these tests; plain_http_get must
    # still work by dialing directly (resolve_and_pin on an IP literal never
    # touches the network).
    monkeypatch.setattr("egress_broker.get_egress_proxy", lambda: None)


# ─────────────────────────── md_fallback primitives ───────────────────────────

def test_is_document_url_covers_all_listed_suffixes():
    for suffix in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".odt", ".rtf"):
        assert md_fallback.is_document_url(f"https://host/report{suffix}")
    assert not md_fallback.is_document_url("https://host/report")
    assert not md_fallback.is_document_url("https://host/page.html")


def test_content_type_suffix_maps_known_document_mimes():
    assert md_fallback.content_type_suffix("application/pdf; charset=binary") == ".pdf"
    assert md_fallback.content_type_suffix("application/msword") == ".doc"
    assert md_fallback.content_type_suffix("text/html") is None
    assert md_fallback.is_document_content_type("application/vnd.ms-excel") is True
    assert md_fallback.is_document_content_type("text/plain") is False


def test_is_navigation_failure_matches_known_markers():
    assert md_fallback.is_navigation_failure("Page.goto: net::ERR_FAILED at ...")
    assert md_fallback.is_navigation_failure("Failed on navigating ACS-GOTO")
    assert md_fallback.is_navigation_failure("Download is starting")
    assert not md_fallback.is_navigation_failure("Blocked by anti-bot protection: HTTP 429")
    assert not md_fallback.is_navigation_failure(None)


# ─────────────────────────── .doc extraction (item B) ───────────────────────────

def test_doc_extraction_never_raises_on_junk_input():
    assert doc_parse.parse_document("file.doc", b"") == ""
    assert doc_parse.parse_document("file.doc", b"\x00\x01\x02\xff\xfe") == ""
    # Garbage that happens to decode to readable ASCII under cp1252 survives;
    # that's expected for a best-effort scrape.
    assert doc_parse.parse_document("file.doc", b"short junk here") == "short junk here"


def test_doc_extraction_recovers_utf16le_text():
    text = "Grant application body text goes here"
    recovered = doc_parse.parse_document("file.doc", text.encode("utf-16-le"))
    assert text in recovered


def test_xls_still_rejected():
    with pytest.raises(doc_parse.ParseError) as exc:
        doc_parse.parse_document("file.xls", b"not a real xls")
    assert exc.value.status == 400


# ─────────────────────────── plain_http_get (items C, E) ───────────────────────────

@pytest.mark.asyncio
async def test_plain_http_get_refuses_redirect_to_private_ip(monkeypatch):
    calls = _install_fake_client(monkeypatch, [
        _FakeResponse(302, headers={"location": "http://127.0.0.1/admin"}),
    ])
    with pytest.raises(md_fallback.PlainFetchError) as exc:
        await md_fallback.plain_http_get("http://93.184.216.34/start")
    assert exc.value.reason == "URL blocked"
    assert len(calls) == 1  # never followed the redirect


@pytest.mark.asyncio
async def test_plain_http_get_refuses_redirect_to_link_local_metadata_ip(monkeypatch):
    _install_fake_client(monkeypatch, [
        _FakeResponse(302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
    ])
    with pytest.raises(md_fallback.PlainFetchError):
        await md_fallback.plain_http_get("http://93.184.216.34/start")


@pytest.mark.asyncio
async def test_plain_http_get_refuses_initial_private_url(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(200)])
    with pytest.raises(md_fallback.PlainFetchError):
        await md_fallback.plain_http_get("http://127.0.0.1/secret")


@pytest.mark.asyncio
async def test_plain_http_get_honors_retry_after_once(monkeypatch):
    calls = _install_fake_client(monkeypatch, [
        _FakeResponse(429, headers={"retry-after": "0.01"}),
        _FakeResponse(200, headers={"content-type": "text/html"}, content=b"<html>ok</html>"),
    ])
    status, headers, content, final_url = await md_fallback.plain_http_get("http://93.184.216.34/page")
    assert status == 200
    assert content == b"<html>ok</html>"
    assert len(calls) == 2  # retried exactly once


@pytest.mark.asyncio
async def test_plain_http_get_follows_safe_redirect(monkeypatch):
    calls = _install_fake_client(monkeypatch, [
        _FakeResponse(301, headers={"location": "http://93.184.216.34/final"}),
        _FakeResponse(200, headers={"content-type": "text/html"}, content=b"<html>final</html>"),
    ])
    status, headers, content, final_url = await md_fallback.plain_http_get("http://93.184.216.34/start")
    assert status == 200
    assert final_url == "http://93.184.216.34/final"
    assert len(calls) == 2


# ─────────────────────────── handle_markdown_request (items A, C, D, F) ───────────────────────────

def _docx_bytes(text: str) -> bytes:
    import io
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_document_url_is_routed_to_parser_without_touching_the_browser(monkeypatch):
    url = "https://host.example/files/report.docx"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)

    import crawler_pool

    async def get_crawler_should_not_be_called(*args, **kwargs):
        raise AssertionError("the browser must not be used for a document URL")

    monkeypatch.setattr(crawler_pool, "get_crawler", get_crawler_should_not_be_called)

    async def fake_fetch(fetch_url, **kwargs):
        assert fetch_url == url
        return 200, {"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, _docx_bytes("Grant funding details"), url

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)

    markdown, source = await api.handle_markdown_request(url, FilterType.FIT, config=_CONFIG)
    assert source == "document"
    assert "Grant funding details" in markdown


@pytest.mark.asyncio
async def test_browser_429_failure_recovered_via_plain_http(monkeypatch):
    url = "https://host.example/grants"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)

    html = "<html><body><p>Recovered page content from plain HTTP</p></body></html>"

    class FailsToNavigateButRendersRawHtml(_FailedCrawler):
        """Mirrors the real crawler: navigating the URL hits the anti-bot
        block, but rendering caller-supplied HTML via raw: (no navigation)
        works fine and goes through the same markdown generator."""

        async def arun(self, url, config=None, **kwargs):
            self.calls.append(url)
            if url.startswith("raw:"):
                text = doc_parse.html_to_markdown(url[len("raw:"):])
                return SimpleNamespace(
                    success=True,
                    markdown=SimpleNamespace(raw_markdown=text, fit_markdown=text),
                )
            return SimpleNamespace(success=False, error_message=self.error_message)

    crawler = FailsToNavigateButRendersRawHtml(
        "Blocked by anti-bot protection: HTTP 429 Too Many Requests"
    )
    _patch_crawler_pool(monkeypatch, crawler)

    async def fake_fetch(fetch_url, **kwargs):
        return 200, {"content-type": "text/html"}, html.encode(), url

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)

    markdown, source = await api.handle_markdown_request(url, FilterType.RAW)
    assert source == "http"
    assert "Recovered page content from plain HTTP" in markdown
    # The initial attempt navigated and hit the anti-bot block; the recovery
    # re-used the browser only to render the plain-HTTP-fetched HTML locally
    # (raw:), never re-navigating to the blocked URL.
    assert crawler.calls == [url, "raw:" + html]


@pytest.mark.asyncio
async def test_non_html_non_document_response_is_not_treated_as_html(monkeypatch):
    """A plain HTTP success that isn't HTML and isn't a recognized document
    type (e.g. an image, or a JSON API response) must not be decoded as
    UTF-8 and fed through the markdown pipeline - that would silently
    return garbled "successful" markdown instead of a clear error."""
    url = "https://host.example/weird-binary"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)

    crawler = _FailedCrawler("Blocked by anti-bot protection: HTTP 403")
    _patch_crawler_pool(monkeypatch, crawler)

    async def fake_fetch(fetch_url, **kwargs):
        return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\n...", url

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)

    with pytest.raises(api.HTTPException) as exc:
        await api.handle_markdown_request(url, FilterType.RAW)

    assert exc.value.status_code == 502
    assert "unsupported content-type: image/png" in exc.value.detail
    # Never asked the browser to render binary bytes as HTML.
    assert crawler.calls == [url]


@pytest.mark.asyncio
async def test_all_fallbacks_fail_gives_combined_502_detail(monkeypatch):
    url = "https://host.example/unreachable"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)

    crawler = _FailedCrawler("net::ERR_TUNNEL_CONNECTION_FAILED")
    _patch_crawler_pool(monkeypatch, crawler)

    async def fake_fetch(fetch_url, **kwargs):
        raise md_fallback.PlainFetchError("request failed: connection refused")

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)
    # No DataImpulse credentials configured (see _offline_defaults) -> proxy stays disabled.

    with pytest.raises(api.HTTPException) as exc:
        await api.handle_markdown_request(url, FilterType.FIT, config=_CONFIG)

    assert exc.value.status_code == 502
    detail = exc.value.detail
    assert "browser: net::ERR_TUNNEL_CONNECTION_FAILED" in detail
    assert "http: request failed: connection refused" in detail
    assert "proxy: disabled" in detail


@pytest.mark.asyncio
async def test_proxy_tiers_are_tried_in_order_when_enabled(monkeypatch):
    url = "https://host.example/needs-different-egress"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)
    _set_tiers(monkeypatch)  # datacenter, residential, premium all configured

    class RecoverOnThirdProxyAttempt(_FailedCrawler):
        async def arun(self, url, config=None, **kwargs):
            self.calls.append(url)
            # call 1 = initial browser attempt (no proxy)
            # calls 2,3,4 = datacenter, residential, premium retries
            if len(self.calls) == 4:
                return SimpleNamespace(
                    success=True,
                    markdown=SimpleNamespace(raw_markdown="recovered via premium", fit_markdown="recovered via premium"),
                )
            return SimpleNamespace(success=False, error_message=self.error_message)

    crawler = RecoverOnThirdProxyAttempt("net::ERR_TUNNEL_CONNECTION_FAILED")
    _patch_crawler_pool(monkeypatch, crawler)

    async def fake_fetch(fetch_url, **kwargs):
        raise md_fallback.PlainFetchError("connection refused")

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)

    markdown, source = await api.handle_markdown_request(url, FilterType.FIT, config=_CONFIG)
    assert source == "proxy-premium"
    assert markdown == "recovered via premium"
    assert len(crawler.calls) == 4  # initial + datacenter + residential + premium, in that order


@pytest.mark.asyncio
async def test_proxy_disabled_without_credentials_skips_straight_to_failure(monkeypatch):
    url = "https://host.example/needs-different-egress"
    monkeypatch.setattr(api, "validate_url_destination", lambda u: None)
    # _offline_defaults already cleared DataImpulse env vars.

    crawler = _FailedCrawler("net::ERR_TUNNEL_CONNECTION_FAILED")
    _patch_crawler_pool(monkeypatch, crawler)

    async def fake_fetch(fetch_url, **kwargs):
        raise md_fallback.PlainFetchError("connection refused")

    monkeypatch.setattr(md_fallback, "plain_http_get", fake_fetch)

    with pytest.raises(api.HTTPException) as exc:
        await api.handle_markdown_request(url, FilterType.FIT, config=_CONFIG)

    assert "proxy: disabled" in exc.value.detail
    assert crawler.calls == [url]  # no proxy retries attempted
