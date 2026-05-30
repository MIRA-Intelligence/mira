# Local Engine Upgrade Runbook

This runbook defines the manual-safe upgrade process for `mira-engine` local deployments.

## Preconditions

- `mira-engine install-service` has already been executed.
- Service status is healthy before upgrade:

```bash
mira-engine status
mira-engine doctor
```

## Standard Upgrade Flow

```bash
mira-engine upgrade --package mira
```

The command performs:

1. Stop service
2. Upgrade package via pip
3. Start service
4. Run `/health` check on `127.0.0.1:46321`

## Automatic Rollback Behavior

If upgrade fails at any step:

- Reinstall previous package version (if known)
- Attempt to restart service with the previous version

## Manual Rollback (Operator Action)

If automated rollback fails, run:

```bash
mira-engine stop
python -m pip install --upgrade mira==<previous_version>
mira-engine start
mira-engine status
mira-engine doctor
```

## Artifacts And Logs

- Service state: `~/.mira/runtime/agent-service-state.json`
- Upgrade backups: `~/.mira/runtime/backups/`
- Service logs: `~/.mira/logs/agent-service.log`
