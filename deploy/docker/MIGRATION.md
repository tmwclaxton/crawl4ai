# Migration Guide — Docker Server Hardening Release

This is a major, **secure-by-default** release of the Crawl4AI **Docker API
server** (`deploy/docker/`). Several defaults changed in breaking ways so the
out-of-the-box deployment is safe. The core pip library (SDK / in-process use)
is **unchanged** — these notes apply only to the self-hosted HTTP server.

How much you have to do scales with how much you drove through the API. A plain
"crawl these URLs with a normal config" user only does the two steps in
**Everyone**. The rest applies only if you used that specific feature.

> Upgrading from a self-hosted server? Read this first, then roll out behind a
> staging environment. See `SECURITY-VERIFY.md` for the deployment checklist.
>
> GrantGunner fork operators: read [UPGRADING](#upgrading)
> before deploying the audit section C image. That section lists every changed
> environment variable, the Redis migration, and the post-deploy checks.

---

## Everyone (2 steps)

### 1. Set an API token

The server no longer serves an unauthenticated API on `0.0.0.0`. It binds
loopback by default and will not expose itself without a credential.

```bash
export CRAWL4AI_API_TOKEN="$(openssl rand -hex 32)"
```

With `docker compose`, run the export in the same shell before
`docker compose up`; the compose file passes the token into the container.
For a persistent setup, put the `CRAWL4AI_API_TOKEN=...` line in a `.env`
file in the project root instead — compose reads it automatically (a shell
export still takes precedence).

> ⚠️ For plain `docker run`, pass it explicitly:
> `-e CRAWL4AI_API_TOKEN="$CRAWL4AI_API_TOKEN"` (the value-less shorthand
> `-e CRAWL4AI_API_TOKEN` silently passes empty from a shell where the variable
> isn't set).

- With a token set, you may expose the server (put a TLS-terminating reverse
  proxy in front) and must send `Authorization: Bearer <token>` on every
  request except `GET /health`.
- With **no** token set, the server binds `127.0.0.1` only (the **container's**
  loopback — published ports answer with *connection reset* even though the
  container reports healthy) and prints a one-off token at startup for
  in-container use.

WebSocket clients (MCP, monitor) that can't set headers may pass `?token=...`.

### 2. Re-issue any tokens

The JWT implementation changed; tokens issued by older versions are no longer
valid. Re-mint via `POST /token` (which now requires the server to have an
`api_token` configured).

---

## Only if you used that feature

### Request bodies accept declarative options only

A crawl request body now carries scalar, declarative options only. The
following are **rejected with HTTP 400** when sent over the network; configure
them server-side or run a self-hosted in-process build (the SDK keeps full
control):

`js_code`, `js_code_before_wait`, `c4a_script`, `proxy` / `proxy_config`,
`extra_args`, `user_data_dir`, `cdp_url`, `cookies`, `headers`, `init_scripts`,
`base_url`, `deep_crawl_strategy`, `simulate_user`, `magic`,
`process_in_browser`, and nested LLM config objects.

`deep_crawl_strategy` can be turned back on by the operator with
`CRAWL4AI_ALLOW_DEEP_CRAWL=true`, with server-side limits. See "Deep Crawling
(opt-in)" in the self-hosting guide.

Unknown fields are dropped; timeouts, viewport and scroll counts are clamped to
safe maximums.

### Hooks: declarative actions instead of code

Hooks are now **disabled by default** — enable them with
`CRAWL4AI_HOOKS_ENABLED=true` in the container environment, or any request
containing `hooks` returns HTTP 403.

`hooks.code` (Python strings) is replaced by a fixed set of declarative actions:

```jsonc
{
  "hooks": {
    "hooks": [
      {"action": "block_resources", "params": {"resource_types": ["image", "font"]}},
      {"action": "scroll_to_bottom",  "params": {"max_steps": 10, "delay_ms": 500}}
    ]
  }
}
```

Available actions: `block_resources`, `add_cookies`, `set_headers`,
`scroll_to_bottom`, `wait_for_timeout`. Call `GET /hooks/info` for the parameter
schemas. Arbitrary hook code is available in a self-hosted in-process build.

> ⚠️ **Legacy `hooks.code` requests fail silently.** With hooks enabled, a
> request in the old format returns HTTP 200 with
> `"hooks": {"status": "success", "attached": []}` — the inline code is
> dropped without error. If `attached` is empty, your hooks did not run.
> (With hooks disabled, the same request returns the generic 403, whose
> "enable hooks" hint will not make code hooks work either.)

### Screenshot / PDF: artifact id instead of `output_path`

`output_path` is removed. The server stores the result and returns an id + URL:

```jsonc
{"success": true, "screenshot": "<base64>",
 "artifact_id": "….", "url": "/artifacts/….", "mime": "image/png", "size": 12345}
```

Fetch the file with `GET /artifacts/{artifact_id}` (authenticated). Artifacts
have a TTL and a storage quota.

> ⚠️ A request that still includes `output_path` is **silently ignored** — it
> returns `success: true` with an artifact id, but no file is written to the
> requested path. Update your code to fetch from `/artifacts/{artifact_id}`.

### LLM endpoints: provider by name

`base_url` is removed from `/md`, `/llm`, and `/llm/job`. Select a provider by
**name** only; the endpoint and key are configured server-side via env
(`OPENAI_BASE_URL` / `LLM_BASE_URL`) and `config.llm.allowed_providers`. A
provider outside the allowed family returns 400.

### Monitor actions need an admin token

`POST /monitor/actions/cleanup|kill_browser|restart_browser` and
`/monitor/stats/reset` require an **admin-scope** principal (the static
`CRAWL4AI_API_TOKEN` is admin; `/token`-issued JWTs are `data` scope).

### Browser / JS clients: allowlist your origin (CORS)

Cross-origin browser requests are denied unless allowlisted:

```yaml
security:
  cors_allow_origins: ["https://your-frontend.example"]
```

### TLS verification is on

Self-signed / internal TLS crawl targets now fail by default. For trusted
internal testing only: `CRAWL4AI_ALLOW_INSECURE_TLS=true`. Internal-network
crawling escape hatch: `CRAWL4AI_ALLOW_INTERNAL_URLS=true`.

### Webhook headers are validated

Custom webhook headers must be well-formed names with no control characters and
may not set hop-by-hop / sensitive headers (`Host`, `Content-Length`,
`Transfer-Encoding`, `Authorization`, `Cookie`, …). Invalid headers → 422.

### Redis requires a password

Redis runs in-container, loopback-only, password-protected, and its port is no
longer published. For an **external** redis, set `REDIS_PASSWORD`.

### Resource limits (all configurable; `0` = unbounded)

```yaml
limits:
  max_body_bytes: 10485760   # request body cap (413); 0 = unbounded
  wall_clock_s: 0            # per-crawl deadline (504); 0 = none
  queue:
    maxsize: 1000            # background job queue (503 when full); 0 = unbounded
    workers: 4
    per_principal: 0         # max concurrent jobs per caller (429); 0 = unlimited
```

To keep the previous behavior exactly, set the caps you don't want to `0`.

### Timeouts from a request are capped at 60s

`page_timeout`, `wait_for_timeout`, and `body_visibility_timeout` arriving in a
request body are clamped to 60000ms, so a client asking for more is given 60s
and its crawl fails with `Page.goto: Timeout 60000ms exceeded`.

That bound is right for a server reachable by untrusted callers. A deployment
that is not public — a crawler on a private network fetching pages that
legitimately take minutes — can raise it:

```bash
CRAWL4AI_MAX_TIMEOUT_MS=300000
```

A request still only gets the timeout it asks for; this sets the ceiling, and
a smaller value tightens it. A value that is not a positive integer is refused
with a warning and the 60000ms default kept, so a typo cannot silently widen
the bound.

Raising this ceiling alone is not enough. Two other deadlines cut a crawl
short first, and both are in `config.yml`:

- `limits.wall_clock_s` (default `300`) — the per-crawl deadline; the request
  gets a 504 at that point no matter what `page_timeout` says.
- `crawler.timeouts.batch_process` (default `300.0`) — the batch crawl budget.

So a 300000ms ceiling needs `wall_clock_s` and `batch_process` raised past 300
too, or the extra timeout can never be reached.

### Error responses are generic

5xx responses return `{"error": "Internal server error", "correlation_id": "…"}`.
Match the correlation id in the server logs for detail. Developer-facing 4xx
messages are unchanged.

---

## Defaults summary

| Setting | Old | New |
| --- | --- | --- |
| Bind | `0.0.0.0`, open | `127.0.0.1`; exposing requires a token |
| Auth | off by default | on by default |
| Security headers / CSP | off | on (strict on the API surface) |
| CORS | none | deny-by-default |
| TLS verification | disabled | enabled |
| Redis | no password, port published | password, loopback, not published |
| `output_path` | accepted | removed (artifact store) |
| LLM `base_url` in request | honored | removed |
| Hooks | Python code | declarative actions |
| Background jobs | unbounded | bounded queue (configurable, 0 = unbounded) |

## Operational notes

- **`--no-sandbox`** is still set by default (the container runs as non-root
  without a usable sandbox). To drop it, run the container with an unprivileged
  user namespace (`unprivileged_userns_clone=1`) or a seccomp profile, then set
  `CRAWL4AI_CHROMIUM_SANDBOX=true`. See `SECURITY-VERIFY.md`.
- The hardened `docker-compose.yml` uses `read_only: true` + tmpfs, `cap_drop:
  [ALL]`, `no-new-privileges`, and `shm_size` instead of a host `/dev/shm` bind.
  Mirror these in a custom compose file.
- The `/dashboard` and `/playground` UIs get baseline headers
  (`nosniff`, `X-Frame-Options: DENY`) and are auth-gated; a stricter CSP for
  the UIs is planned in a follow-up.

---

## UPGRADING

This section is for the self-hosted crawler GrantGunner calls at
`http://crawl4ai:11235`. GrantGunner, the crawl4ai MCP, and gg-agents all use
this server. GrantGunner posts the operator API token to `POST /token` and
then sends the returned JWT on scrape and search calls.

### Environment variables

Set these in the shell or the project `.env` before `docker compose up`.
Compose reads `.env` itself. A shell export wins over `.env`.

| Variable | Default | What changed |
| --- | --- | --- |
| `CRAWL4AI_DISABLE_RATE_LIMITER` | `false` | The same-host crawl pause stays on (about 1 to 2 seconds per host). Set `true` only when a private batch job must turn that pause off. |
| `CRAWL4AI_ALLOW_DEEP_CRAWL` | `false` | Deep crawl is rejected until this is `true`. |
| `CRAWL4AI_PUBLIC_BASE_URL` | `https://crawl4ai.grantgunner.org` | Origin written into the MCP OAuth discovery documents. |
| `CRAWL4AI_TOKEN_RATE_PER_MINUTE` | `60` | Per-minute cap for untrusted JWT subjects, counted separately in each gunicorn worker. A negative value means unlimited. The operator API token, JWTs from `POST /token` (`via=operator`), and `CRAWL4AI_TRUSTED_SUBJECTS` are exempt. |
| `CRAWL4AI_TRUSTED_SUBJECTS` | empty | Comma-separated JWT subjects that skip the per-minute cap and the daily paid caps. Use this to name the backend email. Operator-issued JWTs are already exempt without it. |
| `CRAWL4AI_DAILY_FIRECRAWL_CAP` | `500` | Daily Firecrawl calls per untrusted token. A negative value means unlimited. The trusted backend is exempt. |
| `CRAWL4AI_DAILY_PROXY_CAP` | `1000` | Daily DataImpulse proxy calls per untrusted token. A negative value means unlimited. The trusted backend is exempt. |
| `CRAWL4AI_DAILY_NANOGPT_CAP` | `5000` | Daily NanoGPT calls per untrusted token. A negative value means unlimited. The trusted backend is exempt. |
| `CRAWL4AI_TRUSTED_HOSTS` | empty | Comma-separated extra `Host` names appended to `crawl4ai.grantgunner.org`, `crawl4ai`, `localhost`, `127.0.0.1`, and `testserver`. Add any other name a caller sends, or that caller receives HTTP 400. |
| `REDIS_HOST` | `crawl4ai_redis` | DNS name of this stack's Redis. Do not set it to `redis`. |
| `REDIS_PASSWORD` | none (required) | No default is shipped. `docker compose up` fails immediately with a clear message when this is unset or empty. |
| `REDIS_MAXMEMORY` | `1gb` | Redis `maxmemory`. Eviction policy is `volatile-lru`. |
| `GIT_SHA` | `unknown` | Build arg, not a runtime default you can omit safely. Export `GIT_SHA=$(git rev-parse HEAD)` before `docker compose build`. `/health` reports it as `git_sha`. `unknown` means the image was built without the arg. The image stores it as `CRAWL4AI_GIT_SHA`. |

`mem_limit: 4g` is set on the crawl4ai service (and `deploy.resources.limits.memory: 4G`). That is compose configuration, not an environment variable.

Decorated routes still have the SlowAPI ceiling of `1000/minute` per token. The exemption above is the extra 60/minute middleware and the daily paid caps.

### Redis migration

The service name stays `crawl4ai_redis`. Clients, including GrantGunner, keep using that host. This compose file owns that service. Do not run a second Redis that also answers as `crawl4ai_redis` on a shared network, and do not add a network alias named `redis`.

The existing production `crawl4ai_redis` was passwordless. This file will not start until `REDIS_PASSWORD` is set, and it will not invent a password for you. Set the same value as Redis `requirepass` before the app container starts. If the app sends `AUTH` and Redis has no password, or the other way around, connections fail.

Eviction is `volatile-lru` with `REDIS_MAXMEMORY` defaulting to `1gb` (previously `allkeys-lru` at 256mb). Only keys that already have a TTL can be evicted, and only after maxmemory is reached. Job hashes use `task_ttl_seconds` (default 3600). Keys without a TTL, including monitors and the usage log, are kept. Raise `REDIS_MAXMEMORY` when stored crawl results are large.

Redis data is stored in the named volume `crawl4ai_redis_data`. A volume created on the first boot with this file does not contain an older container's data directory. In-flight job keys expire within the task TTL. Attach the old data directory yourself before the first `up` if those keys must survive the recreate.

The image entrypoint refuses `REDIS_HOST=redis`. When the host is not loopback, it also refuses to start without `REDIS_PASSWORD`. The single-container image, whose Redis is on 127.0.0.1, still generates an ephemeral password for that bundled instance when none was provided.

### Breaking changes

* MCP OAuth refresh tokens last 7 days. They previously lasted 30 days. A client whose refresh token is older than 7 days must sign in again. Access tokens stay at 60 minutes.
* Deep crawl is off unless `CRAWL4AI_ALLOW_DEEP_CRAWL=true`. A request that sets `deep_crawl_strategy` otherwise returns HTTP 400.
* Fetch paths reject internal targets before any request is sent. Blocked targets include `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, link-local, IPv6 equivalents, names with no dot such as `grantgunner-app-1` and `redis`, and suffixes such as `.internal`, `.local`, and `.docker`. Search checks each redirect hop. A private DNS answer is also rejected.

### Edge proxy

`deploy/docker/nginx-edge.conf` is not applied by this compose file. The live edge proxy has to pick it up separately. The directives that matter are `proxy_read_timeout 330s` and `client_max_body_size 10m`.

### Post-deploy checklist

* `POST /token` with the operator API token returns a JWT, and that JWT's `via` claim is `operator`.
* A GrantGunner scrape succeeds.
* A GrantGunner search succeeds.
* 60 or more requests per minute from the backend subject do not return 429. The backend subject is the JWT from `POST /token`, or a subject listed in `CRAWL4AI_TRUSTED_SUBJECTS`.
* An unrelated JWT subject still receives 429 after `CRAWL4AI_TOKEN_RATE_PER_MINUTE` requests in a minute.
* `GET /health` reports a `git_sha` that matches the commit you built.
* Redis answers `AUTH` with the password you set (`redis-cli -a "$REDIS_PASSWORD" ping` inside the Redis container returns `PONG`).
