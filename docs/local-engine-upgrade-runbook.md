# Local Engine Upgrade Runbook

This runbook defines the manual-safe upgrade process for `medpilot-agent` local deployments.

## Preconditions

- `medpilot-agent install-service` has already been executed.
- Service status is healthy before upgrade:

```bash
medpilot-agent status
medpilot-agent doctor
```

## Standard Upgrade Flow

```bash
medpilot-agent upgrade --package medpilot
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
medpilot-agent stop
python -m pip install --upgrade medpilot==<previous_version>
medpilot-agent start
medpilot-agent status
medpilot-agent doctor
```

## Artifacts And Logs

- Service state: `~/.medpilot/runtime/agent-service-state.json`
- Upgrade backups: `~/.medpilot/runtime/backups/`
- Service logs: `~/.medpilot/logs/agent-service.log`
