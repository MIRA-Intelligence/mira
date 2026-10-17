# UI Gateway Authentication

The UI gateway (`mira gateway`, default port 18790) serves the desktop/web UI over
HTTP (`/api/*`) and WebSocket (`/ws`). Those routes can change configuration,
upload files, install skills and drive an agent that runs shell and filesystem
tools, so every request must present the install's **gateway token**.

## Wire contract

| Item | Value |
| --- | --- |
| Token | `secrets.token_urlsafe(32)`, one per install |
| Token file | `<MIRA_HOME>/runtime/gateway-token` (default `~/.mira/runtime/gateway-token`), mode `0600`, created when the gateway starts |
| Precedence | env `MIRA_GATEWAY_TOKEN` > config `gateway.authToken` > token file |
| HTTP | `Authorization: Bearer <token>` |
| Query form | `?access_token=<token>` — for WebSocket and URLs that cannot set headers (`<img>`, `<iframe>`, downloads) |
| WebSocket | `ws://<host>:<port>/ws?access_token=<token>`; rejected before the upgrade with `401` (token) or `403` (origin) |
| Open routes | `GET`/`HEAD` `/health`, `/version`, `/api/health`, `/api/version`, and CORS preflight `OPTIONS` |
| Failure | `401 {"error": "unauthorized"}` with `WWW-Authenticate: Bearer`; `403 {"error": "origin not allowed"}`; CORS headers are kept so browsers can read the status |

Tokens are compared in constant time. The gateway never logs query strings
(the aiohttp access log is disabled; request events record route templates
only), and the token file is not part of diagnostic bundles.

## Getting the token

```bash
mira gateway-token            # print the effective token (creates the file if needed)
mira gateway-token --rotate   # write a new token file; restart the gateway to apply
```

`mira gateway-token` prints only the token on stdout, so scripts can capture it.
When `MIRA_GATEWAY_TOKEN` or `gateway.authToken` is set, that value wins over the
file and `--rotate` warns that the rotated file is not in effect.

The desktop app reads the token file of the engine it manages. Remote and
self-hosted web clients must be given the token (see the UI's engine settings).

## Browser origins

Requests without an `Origin` header (CLI tools, scripts, Electron `fetch` from
`file://`) are accepted when the token is valid. When a browser sends `Origin`,
it must be listed in `channels.ui.corsOrigins`:

```json
{ "channels": { "ui": { "corsOrigins": ["file://", "http://localhost:5173", "http://127.0.0.1:5173"] } } }
```

These are the defaults: `file://` is what the packaged Electron renderer sends on
WebSocket handshakes (measured on Electron 36; its `fetch` calls send no Origin),
and the `5173` entries cover the Vite dev server. Extra origins can be added
without editing the config through `MIRA_GATEWAY_ALLOWED_ORIGINS`
(comma-separated), which the Docker stack uses for the web UI container.

`"*"` is still honored and accepts any website origin; the gateway logs a
warning at startup.

**Upgrade migration.** Older versions saved the whole config, so most existing
`config.json` files contain `"corsOrigins": ["*"]`, the previous default. When
the config is loaded, a value of exactly `["*"]` is replaced with the default
list above (logged once at INFO); the change is written back the next time the
config is saved. Any other list, including one that mixes `"*"` with other
origins, is left as is. To keep accepting any origin, set
`MIRA_GATEWAY_ALLOWED_ORIGINS=*` (or use a mixed list) instead of a bare
`["*"]`.

## Bind address

`gateway.host` now defaults to `127.0.0.1`. Binding any non-loopback address
(for example `--host 0.0.0.0` on a server or in a container) logs a warning:
the gateway is then reachable from the network and protected only by the token,
so put it behind TLS when it leaves a trusted network. Configs written by older
versions may still contain `"host": "0.0.0.0"`; change it to `127.0.0.1` for
desktop-only use.

## Upgrading clients (breaking change)

Clients that do not send the token receive `401` from every `/api/*` route and
cannot open `/ws`. Upgrade mira-ui together with the engine (see
MIRA-Intelligence/mira-ui#68). The service manager (`mira-engine status`)
reads the local token automatically.

The OpenAI-compatible API server (`mira serve`, port 8900) has its own keys and
origin policy; see [api-auth.md](api-auth.md).
