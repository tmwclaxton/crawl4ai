"""Browser OAuth handover for the public Crawl4ai MCP endpoint.

Same shape GrantGunner uses for MCP clients (Cursor, Claude, VS Code):

  * unauthenticated POST /mcp returns 401 with a resource_metadata URL
  * /.well-known/oauth-protected-resource and oauth-authorization-server
  * POST /oauth/register creates a public PKCE client
  * GET /oauth/authorize is the browser prompt
  * POST /oauth/token exchanges the code (and later a refresh token)

The prompt asks for the Crawl4ai API token. A matching token is the only
way to mint an access token. Issued access tokens are normal data-scope
JWTs, so the existing auth gate accepts them and they cannot call admin
monitor actions.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import constant_time_eq, create_access_token

logger = logging.getLogger(__name__)

OAUTH_SCOPE = "mcp:use"
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL_SECONDS = 30 * 24 * 3600
CODE_TTL_SECONDS = 600
_CUSTOM_SCHEMES = {"cursor", "vscode", "vscode-insiders", "claude"}

OAUTH_PUBLIC_PATHS = {
    "/oauth/register",
    "/oauth/authorize",
    "/oauth/token",
}
OAUTH_PUBLIC_PREFIXES = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


@dataclass
class _Client:
    client_id: str
    client_name: str
    redirect_uris: list[str]


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    expires_at: float


@dataclass
class _Refresh:
    client_id: str
    expires_at: float


@dataclass
class _Store:
    clients: dict[str, _Client] = field(default_factory=dict)
    codes: dict[str, _Code] = field(default_factory=dict)
    refresh: dict[str, _Refresh] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_store = _Store()


def _expected_api_token() -> str:
    import server

    token = server._current_api_token()
    return token if isinstance(token, str) else ""


def external_base(scope_or_request: Any) -> str:
    """Public origin for metadata. CRAWL4AI_PUBLIC_BASE_URL wins when set."""
    override = os.environ.get("CRAWL4AI_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if override:
        return override

    headers: dict[str, str] = {}
    scheme = "http"
    if isinstance(scope_or_request, Request):
        scheme = scope_or_request.url.scheme or "http"
        for key, value in scope_or_request.headers.items():
            headers[key.lower()] = value
    else:
        scheme = (scope_or_request or {}).get("scheme") or "http"
        for name, value in (scope_or_request or {}).get("headers", []):
            headers[name.decode().lower()] = value.decode()

    proto = headers.get("x-forwarded-proto", scheme).split(",")[0].strip() or "http"
    host = headers.get("x-forwarded-host", headers.get("host", "localhost")).split(",")[0].strip()
    return f"{proto}://{host}"


def resource_metadata_url(scope: dict, path: str) -> str:
    suffix = path.strip("/")
    base = external_base(scope)
    return f"{base}/.well-known/oauth-protected-resource/{suffix}"


def _metadata_resource(base: str, path: str) -> dict:
    resource = base if path == "" else f"{base}/{path.strip('/')}"
    return {
        "resource": resource,
        "authorization_servers": [base],
        "scopes_supported": [OAUTH_SCOPE],
    }


def _authorization_server_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [OAUTH_SCOPE],
        "grant_types_supported": ["authorization_code", "refresh_token"],
    }


def _redirect_allowed(uri: str) -> bool:
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.netloc:
        return True
    if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        return True
    if parts.scheme in _CUSTOM_SCHEMES and (parts.netloc or parts.path):
        return True
    return False


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64_url(digest)


def base64_url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _form_page(
    *,
    client_name: str,
    error: str | None,
    fields: dict[str, str],
) -> HTMLResponse:
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}" />'
        for key, value in fields.items()
    )
    message = ""
    if error:
        message = f'<p class="error">{html.escape(error)}</p>'
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Connect {html.escape(client_name)} to Crawl4ai</title>
<style>
  body {{ margin: 0; font-family: Georgia, "Times New Roman", serif; background: #f4f1ea; color: #1c1915; }}
  main {{ max-width: 28rem; margin: 8vh auto; padding: 1.5rem; background: #fffdf8; border: 1px solid #e4dccb; }}
  h1 {{ font-size: 1.4rem; font-weight: 600; margin: 0 0 0.5rem; }}
  p {{ line-height: 1.45; }}
  label {{ display: block; font-size: 0.9rem; margin: 1rem 0 0.35rem; }}
  input[type="password"] {{ width: 100%; box-sizing: border-box; padding: 0.55rem 0.6rem; font-size: 1rem; }}
  button {{ margin-top: 1rem; padding: 0.55rem 0.9rem; font-size: 1rem; cursor: pointer; }}
  .error {{ color: #8f1d1d; }}
</style>
</head>
<body>
<main>
  <h1>Authorize {html.escape(client_name)}</h1>
  <p>{html.escape(client_name)} wants to read public pages through Crawl4ai. Enter the Crawl4ai API key to allow it. The key is checked on this server and is not sent to the app.</p>
  {message}
  <form method="post" action="/oauth/authorize">
    {hidden}
    <label for="api_key">API key</label>
    <input id="api_key" name="api_key" type="password" autocomplete="off" required />
    <button type="submit">Authorize</button>
  </form>
</main>
</body>
</html>
"""
    status = 401 if error else 200
    return HTMLResponse(body, status_code=status)


def _oauth_error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
    )


def _issue_tokens(client_id: str) -> dict:
    access = create_access_token(
        {"sub": "mcp-client", "jti": secrets.token_hex(8)},
        scope="data",
        expires_delta=ACCESS_TTL,
    )
    refresh = secrets.token_urlsafe(32)
    with _store.lock:
        _store.refresh[refresh] = _Refresh(
            client_id=client_id,
            expires_at=time.time() + REFRESH_TTL_SECONDS,
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "refresh_token": refresh,
        "scope": OAUTH_SCOPE,
    }


def attach_oauth(app: FastAPI) -> None:
    @app.get("/.well-known/oauth-protected-resource")
    async def protected_resource_root(request: Request):
        return _metadata_resource(external_base(request), "")

    @app.get("/.well-known/oauth-protected-resource/{path:path}")
    async def protected_resource_nested(path: str, request: Request):
        return _metadata_resource(external_base(request), path)

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_root(request: Request):
        return _authorization_server_metadata(external_base(request))

    @app.get("/.well-known/oauth-authorization-server/{path:path}")
    async def authorization_server_nested(path: str, request: Request):
        return _authorization_server_metadata(external_base(request))

    @app.post("/oauth/register")
    async def register(request: Request):
        try:
            body = await request.json()
        except Exception:
            return _oauth_error(400, "invalid_client_metadata", "Registration body must be JSON.")
        if not isinstance(body, dict):
            return _oauth_error(400, "invalid_client_metadata", "Registration body must be a JSON object.")

        name = body.get("client_name") or "MCP client"
        if not isinstance(name, str) or len(name) > 120:
            return _oauth_error(400, "invalid_client_metadata", "client_name is invalid.")

        redirects = body.get("redirect_uris")
        if not isinstance(redirects, list) or not redirects or len(redirects) > 8:
            return _oauth_error(400, "invalid_redirect_uri", "redirect_uris must be a short list.")
        if not all(isinstance(uri, str) and _redirect_allowed(uri) for uri in redirects):
            return _oauth_error(400, "invalid_redirect_uri", "A redirect URI is not allowed.")

        client_id = secrets.token_urlsafe(24)
        with _store.lock:
            _store.clients[client_id] = _Client(client_id, name.strip(), list(redirects))
        logger.info("MCP OAuth client registered")
        return JSONResponse(
            {
                "client_id": client_id,
                "client_name": name.strip(),
                "redirect_uris": list(redirects),
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": OAUTH_SCOPE,
            },
            status_code=201,
        )

    @app.get("/oauth/authorize")
    async def authorize_form(request: Request):
        params, error = _authorize_params(request.query_params)
        if error is not None or params is None:
            return _oauth_error(400, "invalid_request", error or "Invalid authorization request.")
        return _form_page(client_name=params["client_name"], error=None, fields=params["fields"])

    @app.post("/oauth/authorize")
    async def authorize_submit(request: Request):
        form = await request.form()
        params, error = _authorize_params(form)
        if error is not None or params is None:
            return _oauth_error(400, "invalid_request", error or "Invalid authorization request.")

        supplied = str(form.get("api_key") or "")
        expected = _expected_api_token()
        if not expected or not supplied or not constant_time_eq(supplied, expected):
            logger.info("MCP OAuth API key rejected")
            return _form_page(
                client_name=params["client_name"],
                error="That API key was not accepted.",
                fields=params["fields"],
            )

        code = secrets.token_urlsafe(32)
        with _store.lock:
            _store.codes[code] = _Code(
                client_id=params["client_id"],
                redirect_uri=params["redirect_uri"],
                challenge=params["code_challenge"],
                expires_at=time.time() + CODE_TTL_SECONDS,
            )
        location = _with_query(params["redirect_uri"], {"code": code, "state": params["state"]})
        return RedirectResponse(location, status_code=302)

    @app.post("/oauth/token")
    async def token(request: Request):
        form = await request.form()
        grant = str(form.get("grant_type") or "")
        client_id = str(form.get("client_id") or "")
        with _store.lock:
            client = _store.clients.get(client_id)
        if client is None:
            return _oauth_error(401, "invalid_client", "Unknown client_id.")

        if grant == "authorization_code":
            return _exchange_code(client, form)
        if grant == "refresh_token":
            return _exchange_refresh(client, form)
        return _oauth_error(400, "unsupported_grant_type", "Use authorization_code or refresh_token.")


def _authorize_params(source: Any) -> tuple[dict | None, str | None]:
    def get(name: str) -> str:
        value = source.get(name)
        return value if isinstance(value, str) else ""

    client_id = get("client_id")
    redirect_uri = get("redirect_uri")
    response_type = get("response_type")
    challenge = get("code_challenge")
    method = get("code_challenge_method") or "S256"
    state = get("state")
    scope = get("scope") or OAUTH_SCOPE

    with _store.lock:
        client = _store.clients.get(client_id)
    if client is None:
        return None, "Unknown client_id."
    if response_type != "code":
        return None, "response_type must be code."
    if redirect_uri not in client.redirect_uris:
        return None, "redirect_uri is not registered for this client."
    if method != "S256" or len(challenge) < 43:
        return None, "PKCE S256 code_challenge is required."
    if scope != OAUTH_SCOPE:
        return None, "scope must be mcp:use."

    fields = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": OAUTH_SCOPE,
    }
    return {
        "client_id": client_id,
        "client_name": client.client_name,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "state": state,
        "fields": fields,
    }, None


def _exchange_code(client: _Client, form: Any) -> JSONResponse:
    code = str(form.get("code") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    verifier = str(form.get("code_verifier") or "")
    with _store.lock:
        pending = _store.codes.pop(code, None)
    if pending is None or pending.expires_at < time.time():
        return _oauth_error(400, "invalid_grant", "Authorization code is invalid or expired.")
    if pending.client_id != client.client_id or pending.redirect_uri != redirect_uri:
        return _oauth_error(400, "invalid_grant", "Authorization code does not match this client.")
    digest = _s256(verifier)
    if (
        not 43 <= len(verifier) <= 128
        or len(digest) != len(pending.challenge)
        or not constant_time_eq(digest, pending.challenge)
    ):
        return _oauth_error(400, "invalid_grant", "PKCE verification failed.")
    return JSONResponse(_issue_tokens(client.client_id))


def _exchange_refresh(client: _Client, form: Any) -> JSONResponse:
    raw = str(form.get("refresh_token") or "")
    with _store.lock:
        current = _store.refresh.pop(raw, None)
    if current is None or current.expires_at < time.time() or current.client_id != client.client_id:
        return _oauth_error(400, "invalid_grant", "Refresh token is invalid or expired.")
    return JSONResponse(_issue_tokens(client.client_id))


def _with_query(uri: str, extra: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in extra.items():
        if value:
            query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
