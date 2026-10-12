"""Browser OAuth handover for https://crawl4ai.grantgunner.org/mcp."""

import base64
import hashlib
import secrets

from auth import create_access_token, decode_token


API_KEY = "unit-test-api-token"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _patch_token(monkeypatch, server_module):
    monkeypatch.setattr(server_module, "_current_api_token", lambda: API_KEY)


def test_mcp_without_a_credential_advertises_oauth(stock_client):
    response = stock_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})

    assert response.status_code == 401
    header = response.headers.get("www-authenticate", "")
    assert 'realm="mcp"' in header
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in header


def test_other_routes_do_not_advertise_the_mcp_metadata(stock_client):
    response = stock_client.post("/crawl", json={})

    assert response.status_code == 401
    assert "resource_metadata" not in response.headers.get("www-authenticate", "")


def test_discovery_documents(stock_client):
    resource = stock_client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json()["resource"] == "http://testserver/mcp"
    assert resource.json()["authorization_servers"] == ["http://testserver"]
    assert resource.json()["scopes_supported"] == ["mcp:use"]

    server = stock_client.get("/.well-known/oauth-authorization-server")
    body = server.json()
    assert body["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert body["token_endpoint"] == "http://testserver/oauth/token"
    assert body["registration_endpoint"] == "http://testserver/oauth/register"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "authorization_code" in body["grant_types_supported"]


def test_api_key_prompt_issues_a_data_scope_token(stock_client, server_module, monkeypatch):
    _patch_token(monkeypatch, server_module)
    registered = stock_client.post(
        "/oauth/register",
        json={
            "client_name": "Cursor",
            "redirect_uris": ["http://127.0.0.1:8787/callback"],
        },
    )
    assert registered.status_code == 201
    assert registered.json()["token_endpoint_auth_method"] == "none"
    client_id = registered.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = _challenge(verifier)
    query = {
        "client_id": client_id,
        "redirect_uri": "http://127.0.0.1:8787/callback",
        "response_type": "code",
        "scope": "mcp:use",
        "state": "abc",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    form = stock_client.get("/oauth/authorize", params=query)
    assert form.status_code == 200
    assert "API key" in form.text
    assert "Cursor" in form.text

    rejected = stock_client.post(
        "/oauth/authorize",
        data={**query, "api_key": "nope"},
        follow_redirects=False,
    )
    assert rejected.status_code == 401
    assert "not accepted" in rejected.text

    accepted = stock_client.post(
        "/oauth/authorize",
        data={**query, "api_key": API_KEY},
        follow_redirects=False,
    )
    assert accepted.status_code == 302
    location = accepted.headers["location"]
    assert location.startswith("http://127.0.0.1:8787/callback?")
    code = location.split("code=", 1)[1].split("&", 1)[0]

    token = stock_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:8787/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    body = token.json()
    claims = decode_token(body["access_token"])
    assert claims["scope"] == "data"
    assert body["scope"] == "mcp:use"

    refreshed = stock_client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": body["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != body["access_token"]


def test_execute_js_is_not_an_mcp_tool(stock_client):
    token = create_access_token({"sub": "tester"}, scope="data")
    response = stock_client.get("/mcp/schema", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "md" in names
    assert "crawl" in names
    assert "html" in names
    assert "execute_js" not in names


def test_registration_rejects_unsafe_redirects(stock_client):
    response = stock_client.post(
        "/oauth/register",
        json={"client_name": "Bad", "redirect_uris": ["javascript:alert(1)"]},
    )
    assert response.status_code == 400
