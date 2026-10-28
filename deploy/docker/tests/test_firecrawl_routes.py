"""Route registration and auth checks for the new tools."""

API_KEY = "unit-test-api-token"


def _auth(monkeypatch, server_module):
    monkeypatch.setenv("CRAWL4AI_API_TOKEN", API_KEY)


def test_new_tools_are_registered_for_mcp(server_module):
    names = set()
    for route in server_module.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if getattr(endpoint, "__mcp_kind__", None) == "tool":
            names.add(endpoint.__mcp_name__)
    for name in (
        "md", "html", "screenshot", "pdf", "crawl", "ask",
        "scrape", "map", "search", "site_crawl", "agent", "parse", "download",
        "monitor_create", "monitor_list", "monitor_check", "monitor_delete",
    ):
        assert name in names


def test_new_routes_require_a_credential(stock_client):
    for path in ("/scrape", "/map", "/search", "/site-crawl", "/agent", "/parse", "/download", "/monitor/list"):
        response = stock_client.post(path, json={})
        assert response.status_code == 401, path


def test_scrape_and_parse_reject_bad_input(stock_client, server_module, monkeypatch):
    _auth(monkeypatch, server_module)
    headers = {"Authorization": f"Bearer {API_KEY}"}
    scrape = stock_client.post("/scrape", json={"urls": ["javascript:alert(1)"]}, headers=headers)
    assert scrape.status_code == 400
    # .doc now gets a best-effort text scrape (no longer rejected); .xls
    # still is, so use that to exercise /parse's bad-input rejection.
    parsed = stock_client.post(
        "/parse",
        json={"filename": "old.xls", "data_base64": "bm90IGEgZG9j"},
        headers=headers,
    )
    assert parsed.status_code == 400
    monitor = stock_client.post(
        "/monitor/create",
        json={
            "name": "Local",
            "urls": ["https://example.com"],
            "schedule": "hourly",
            "webhook": "https://127.0.0.1/hook",
        },
        headers=headers,
    )
    assert monitor.status_code == 400
