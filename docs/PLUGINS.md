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

## Trust tiers

| Tier | Who | Loads | May use |
|---|---|---|---|
| `core-bundled` | `plugins/` in this repo | in-process, on by default | everything incl. `mutate` hooks |
| `verified` | reviewed community plugins (platform registry, upcoming) | in-process | everything incl. `mutate` hooks |
| `community` | anything a user installs | in-process, **off until enabled** | channels, tools, skills, `observe` hooks |
| `experimental` | new / low-trust plugins | out-of-process runner (upcoming) | tools, skills |

A manifest cannot raise its own tier. `observe` hooks receive a snapshot of the
loop state and cannot change messages or the final answer. Permissions are
recorded and shown today; they are enforced once plugins run out of process.

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
