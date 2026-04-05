# Self-hosted Docker Guide (Optional)

This guide is for advanced users and operators.

Default user path remains:

- Cloud hosted usage
- Desktop local engine (`medpilot-agent`) without Docker

Use Docker only when you explicitly want self-hosted infrastructure management.

## 1) Prepare configuration

```bash
cd deploy
cp .env.example .env
```

Adjust tags and ports in `.env` as needed.

## 2) Start stack

```bash
docker compose pull
docker compose up -d
```

## 3) Verify services

```bash
curl http://127.0.0.1:18790/health
curl http://127.0.0.1:18790/version
```

UI default URL:

- `http://127.0.0.1:8080`

## 4) Upgrade

```bash
docker compose pull
docker compose up -d
```

## 5) Rollback

1. Pin previous image tags in `.env`:
   - `MEDPILOT_AGENT_TAG=<previous_tag>`
   - `MEDPILOT_UI_TAG=<previous_tag>`
2. Recreate services:

```bash
docker compose pull
docker compose up -d
```

## 6) Stop stack

```bash
docker compose down
```
