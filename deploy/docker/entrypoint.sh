#!/usr/bin/env bash
# entrypoint.sh - resolve the socket-level egress/auth posture before supervisord.
#
# This is the authoritative socket-level guard (gunicorn binds the socket, not
# Python). It must agree with the in-process _resolve_auth() check in server.py.
set -euo pipefail

# --- Redis password: prefer a mounted secret, else an existing env var. ------
# There is no public default. An external Redis (compose service
# crawl4ai_redis) must be given REDIS_PASSWORD explicitly, below, after the
# host is known. The bundled loopback Redis still gets an ephemeral password
# when the operator did not set one.
if [[ -z "${REDIS_PASSWORD:-}" && -f /run/secrets/redis_password ]]; then
    REDIS_PASSWORD="$(cat /run/secrets/redis_password)"
fi

# --- Redis host: never the bare name "redis" --------------------------------
# config.yml uses crawl4ai_redis. On a shared Docker network the name "redis"
# is ambiguous (this stack and another stack both answer to it). A single
# container has no such service, so fall back to the bundled loopback Redis.
REDIS_HOST="${REDIS_HOST:-}"
if [[ -z "${REDIS_HOST}" ]]; then
    REDIS_HOST="$(python3 -c 'import yaml; print((yaml.safe_load(open("config.yml")) or {}).get("redis", {}).get("host", "127.0.0.1"))')"
fi
if [[ "${REDIS_HOST}" == "redis" ]]; then
    echo "entrypoint: refusing REDIS_HOST=redis. Use crawl4ai_redis, or 127.0.0.1 for the bundled Redis." >&2
    exit 1
fi
if [[ "${REDIS_HOST}" != "127.0.0.1" && "${REDIS_HOST}" != "localhost" && "${REDIS_HOST}" != "::1" ]]; then
    if ! getent hosts "${REDIS_HOST}" >/dev/null 2>&1; then
        echo "entrypoint: Redis host ${REDIS_HOST} did not resolve; using bundled 127.0.0.1." >&2
        REDIS_HOST="127.0.0.1"
    fi
fi
export REDIS_HOST

# External Redis must have an explicit password. Generating one here would
# not match the passwordless production instance, and AUTH would fail with
# a password the operator never set. Loopback keeps the ephemeral password
# so the single-container image stays closed.
if [[ "${REDIS_HOST}" != "127.0.0.1" && "${REDIS_HOST}" != "localhost" && "${REDIS_HOST}" != "::1" ]]; then
    if [[ -z "${REDIS_PASSWORD:-}" ]]; then
        echo "entrypoint: REDIS_PASSWORD is required when REDIS_HOST=${REDIS_HOST}. There is no default password. Set REDIS_PASSWORD to the password configured on that Redis (requirepass) and restart." >&2
        exit 1
    fi
elif [[ -z "${REDIS_PASSWORD:-}" ]]; then
    REDIS_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    echo "entrypoint: no REDIS_PASSWORD provided; generated an ephemeral one for the bundled Redis." >&2
fi
export REDIS_PASSWORD

# --- API token: prefer a mounted secret, else an existing env var. -----------
if [[ -z "${CRAWL4AI_API_TOKEN:-}" && -f /run/secrets/api_token ]]; then
    export CRAWL4AI_API_TOKEN="$(cat /run/secrets/api_token)"
fi

# --- Bind resolution: loopback unless a credential is present. ---------------
PORT="${CRAWL4AI_PORT:-11235}"
if [[ -n "${CRAWL4AI_API_TOKEN:-}" || "${CRAWL4AI_JWT_ENABLED:-false}" == "true" ]]; then
    # A credential is configured -> the operator may expose all interfaces.
    GUNICORN_BIND="${GUNICORN_BIND:-[::]:${PORT}}"
else
    # No credential -> refuse to expose; serve loopback only.
    GUNICORN_BIND="127.0.0.1:${PORT}"
    echo "entrypoint: no CRAWL4AI_API_TOKEN set; binding loopback only (${GUNICORN_BIND})." >&2
    echo "entrypoint: WARNING: this is the CONTAINER's loopback - published ports (-p ${PORT}:${PORT}) will NOT work; connections from the host will be reset." >&2
    echo "entrypoint: to make the server reachable, set CRAWL4AI_API_TOKEN (docker run -e CRAWL4AI_API_TOKEN=..., or 'export CRAWL4AI_API_TOKEN=...' before 'docker compose up'), then restart. (If you enabled security.jwt_enabled in a custom config.yml, set CRAWL4AI_JWT_ENABLED=true instead.)" >&2
fi
export GUNICORN_BIND

exec supervisord -c supervisord.conf --pidfile /tmp/supervisord.pid
