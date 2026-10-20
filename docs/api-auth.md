# API Server Authentication

`mira serve` (default `127.0.0.1:8900`) exposes an OpenAI-compatible API:
`POST /v1/chat/completions` and `GET /v1/models`. Requests drive the same agent
as the UI, including shell and filesystem tools, so every `/v1` request must
present an API key. It is separate from the UI gateway (`mira gateway`, port
18790), which mira-ui uses; see [gateway-auth.md](gateway-auth.md).

## Wire contract

| Item | Value |
| --- | --- |
| Header | `Authorization: Bearer <key>` (query-string keys are not accepted) |
| Keys | env `MIRA_API_KEYS` (comma-separated) > config `api.keys` > the install's gateway token |
| Open routes | `GET`/`HEAD` `/health` |
| Browser origins | Rejected unless listed in `api.corsOrigins` or `MIRA_API_ALLOWED_ORIGINS`; none are allowed by default |
| Failure | `401` with `WWW-Authenticate: Bearer` and an OpenAI-style error (`type: authentication_error`); `403` (`type: permission_error`) for a foreign origin |

Keys are compared in constant time against every configured key, so several
clients can hold different keys and each can be revoked by removing it.

## Keys

With no `MIRA_API_KEYS` and no `api.keys`, the server accepts the install's
gateway token, so a local client works without extra setup:

```bash
curl -s http://127.0.0.1:8900/v1/chat/completions \
  -H "Authorization: Bearer $(mira gateway-token)" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hello"}]}'
```

OpenAI SDKs send the key as a bearer token, so pass it as `api_key`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8900/v1", api_key="<key>")
```

To give clients their own keys instead, configure them (this disables the
gateway-token fallback):

```json
{ "api": { "keys": ["<key-for-script-a>", "<key-for-script-b>"] } }
```

Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
At startup the server logs how many keys it accepts and their source, never the
keys themselves.

## Browser origins

Requests without an `Origin` header (curl, SDKs, scripts) are checked only for
the key. Requests from a web page carry `Origin` and are refused with `403`
unless that origin is allowlisted; this stops any website you visit from
calling the local API. To let a browser app call `/v1` directly:

```json
{ "api": { "corsOrigins": ["http://localhost:3000"] } }
```

or add origins with `MIRA_API_ALLOWED_ORIGINS` (comma-separated). Allowed
origins get CORS headers, including on `401` responses, and `OPTIONS`
preflights are answered without a key. `"*"` accepts any origin and logs a
warning at startup.

## Bind address

`api.host` defaults to `127.0.0.1`. Binding a non-loopback address logs a
warning: the API is then reachable from the network and protected only by its
keys, so put it behind TLS when it leaves a trusted network.

## Upgrading clients (breaking change)

Clients that called `/v1` without a key now receive `401`. Set the client's API
key to the output of `mira gateway-token`, or to one of the configured
`api.keys`.
