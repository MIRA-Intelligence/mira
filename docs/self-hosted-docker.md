# Self-hosted Docker Guide (Optional)

This guide is for advanced users and operators.

Default user path remains:

- Cloud hosted usage
- Desktop local engine (`mira-engine`) without Docker

Use Docker only when you explicitly want self-hosted infrastructure management.

## 1) Prepare configuration

```bash
cd deploy
cp .env.example .env
```

Adjust tags and ports in `.env` as needed.

The engine requires a gateway token on every API and WebSocket request
(see [gateway-auth.md](gateway-auth.md)). Either set a fixed token in `.env`:

```bash
MIRA_GATEWAY_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
```

or leave it empty and read the generated one after the first start (step 3).
If the UI is served from a host or port other than `localhost:8080`, set
`MIRA_GATEWAY_ALLOWED_ORIGINS` to its browser origin, e.g.
`MIRA_GATEWAY_ALLOWED_ORIGINS=https://mira.example.com`.

## 2) Start stack

```bash
docker compose --profile self-hosted pull
docker compose --profile self-hosted up -d
```

## 3) Verify services

```bash
curl http://127.0.0.1:18790/health
curl http://127.0.0.1:18790/version

# Effective token (generated in the data volume when MIRA_GATEWAY_TOKEN is empty)
docker compose --profile self-hosted exec mira-engine mira gateway-token
curl -H "Authorization: Bearer <token>" http://127.0.0.1:18790/api/status
```

Enter the token in the UI's engine connection settings. The engine listens on
`0.0.0.0` inside the container, but compose publishes ports 18790 (engine) and
8080 (UI) on the host's **loopback only** (`127.0.0.1`).

UI default URL (on the Docker host):

- `http://127.0.0.1:8080`

## 3a) Access from another machine

Direct plaintext access to the engine port from other machines is deprecated
(see [remote-access.md](remote-access.md#deprecation-timeline)); do not change
the published ports to `0.0.0.0`. Use one of:

- **SSH tunnel (recommended).** The web UI's browser code calls the engine
  directly, so forward both ports:

  ```bash
  ssh -N -L 8080:127.0.0.1:8080 -L 18790:127.0.0.1:18790 user@docker-host
  ```

  Then open `http://localhost:8080`. The default `MIRA_API_URL`, `MIRA_WS_URL`
  and `MIRA_GATEWAY_ALLOWED_ORIGINS` already match this setup. The desktop app
  only needs the `18790` forward (API URL `http://127.0.0.1:18790/api`,
  WebSocket URL `ws://127.0.0.1:18790/ws`).

- **TLS reverse proxy on the Docker host.** Proxy `127.0.0.1:18790` (and the UI)
  behind HTTPS, set `MIRA_API_URL` / `MIRA_WS_URL` to the `https://` / `wss://`
  addresses, and add the UI's browser origin to `MIRA_GATEWAY_ALLOWED_ORIGINS`.
  A minimal Caddy example is in [remote-access.md](remote-access.md#if-the-engine-must-be-reachable-over-a-network-tls-only).

For teams, the long-term replacement for this stack is the Mira Server
deployment (#159).

## 4) Upgrade

```bash
docker compose --profile self-hosted pull
docker compose --profile self-hosted up -d
```

## 5) Rollback

1. Pin previous image tags in `.env`:
   - `MIRA_AGENT_TAG=<previous_tag>`
   - `MIRA_UI_TAG=<previous_tag>`
2. Recreate services:

```bash
docker compose --profile self-hosted pull
docker compose --profile self-hosted up -d
```

## 6) Stop stack

```bash
docker compose --profile self-hosted down
```
