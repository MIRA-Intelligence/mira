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

Enter the token in the UI's engine connection settings. The engine container
listens on `0.0.0.0` inside the container; publish port 18790 only to networks
you trust, and terminate TLS in front of it for remote access.

UI default URL:

- `http://127.0.0.1:8080`

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
