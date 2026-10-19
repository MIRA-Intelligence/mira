# Plugins

Everything optional in mira — channels, tools, hooks, skill packs — can be a
**plugin**: a directory with a `plugin.json` manifest and its code, loaded by
the plugin kernel (`mira_engine/plugins`). First-party plugins live in the
repo's [`plugins/`](../plugins) directory and use exactly the same contract as
plugins users install into `~/.mira/plugins`.

> Channels distributed as Python packages via `entry_points`
> ([CHANNEL_PLUGIN_GUIDE.md](CHANNEL_PLUGIN_GUIDE.md)) keep working; they load
> without the manifest, tiers and guards described here.

## Quick start

```bash
mira plugin new my-tool --kind tool      # scaffold into ~/.mira/plugins/my-tool
mira plugin test ~/.mira/plugins/my-tool # contract checks + the plugin's own tests
mira plugin enable my-tool               # community plugins are off by default
mira plugin list                         # status, tier and reason for every plugin
```

## Manifest

```json
{
  "id": "my-tool",
  "version": "0.1.0",
  "plugin_api": ">=1.0,<2.0",
  "description": "What it does.",
  "provides": {
    "channels": [{ "name": "mychat", "entry": "chat:MyChatChannel" }],
    "tools":    [{ "name": "my_tool", "entry": "tool:MyTool" }],
    "hooks":    [{ "entry": "hooks:Audit", "mode": "observe" }],
    "skills":   [{ "id": "howto", "path": "skills/howto" }]
  },
  "permissions": { "network": ["api.example.com"], "filesystem": "none", "shell": false, "secrets": [] },
  "isolation": "in-process",
  "default_enabled": false
}
```

`entry` is `module:attr` inside the plugin directory; the directory is imported
as its own package, so relative imports work.

## What the kernel guarantees

| Failure | Effect |
|---|---|
| invalid manifest, API mismatch, import error, wrong entry type | that plugin is not loaded (`mira plugin list` shows why); everything else runs |
| a tool raises or exceeds its timeout (60 s) | the model gets a tool error |
| a hook raises or exceeds its timeout (2 s) | the call is skipped |
| 3 consecutive failures | the plugin is **tripped** and stays off until `mira plugin enable` |
| a plugin tool/channel reuses a built-in name | the built-in wins |

## Remote disable

The plugin registry can switch a plugin version off on every install
(`GET <registry>/plugins/disabled`). The gateway polls it
(`plugins.registry_url`, every `plugins.poll_interval_s`, on unless
`plugins.remote_disable` is false): a listed version's tools and hooks stop
at once, it is not loaded on the next start, and a channel it provides stops
at the next gateway restart. When the registry lifts it, it comes back on its
own. `mira plugin list` shows `remote-disabled` with the registry's reason.

## Trust tiers

| Tier | Who | Runs | May use |
|---|---|---|---|
| `core-bundled` | `plugins/` in this repo | in-process, on by default | everything incl. `mutate` hooks |
| `verified` | issued by the plugin registry after review | in-process | everything incl. `mutate` hooks |
| `community` | issued by the registry | in-process, **off until enabled** | channels, tools, skills, `observe` hooks |
| `experimental` | default for anything in `~/.mira/plugins` the registry hasn't rated | **out of process, sandboxed** | tools, skills |

A manifest cannot raise its own tier: user plugins get the tier the registry
issued (`GET <registry>/plugins`, synced by the gateway's poller, applied on
the next start), else `plugins.default_user_tier`. Promotion is
`experimental → community → verified` by a maintainer
(`PUT /plugins/:id` on the platform).

### Out-of-process plugins

An experimental plugin's tools are served by `python -m mira_engine.plugins.host`
in a child process. With bubblewrap (Linux) the host sees system directories
and its own code read-only, a private scratch directory, the workspace only if
`permissions.filesystem` asks (`workspace:read` / `workspace:write`), and **no
network** unless `permissions.network` is non-empty; the Mira home (config,
keys, gateway token) is masked. The host gets a minimal environment (no API
keys), a memory limit, and is killed when a call hangs; a crash fails that call
and the next call starts a fresh host.

Without bubblewrap (e.g. macOS) experimental plugins don't load unless
`plugins.unsandboxed_experimental` is set — they then still run out of process
(crashes and hangs stay isolated), but their permissions are not enforced.

## Contract

`mira_engine.plugins.testing` holds the checks every plugin must pass
(manifest, API range, loading, channels constructible and stoppable without
starting, tool schemas, hooks bounded and non-raising). Use it in a plugin's
own tests:

```python
from pathlib import Path
from mira_engine.plugins.testing import assert_plugin_contract

def test_contract():
    assert_plugin_contract(Path(__file__).resolve().parents[1])
```

Every plugin in `plugins/` is checked by `tests/plugins/test_contract_kit.py`.
