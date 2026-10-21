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
  "default_enabled": false,
  "requires": ["some_sdk"]
}
```

`requires` lists importable modules the plugin needs (its SDKs). When one is
missing the plugin is **unavailable** (`mira plugin list` says which) instead
of failing — e.g. the matrix channel without the `matrix` extra, or any
messaging channel in the desktop build, which doesn't ship their SDKs.

The messaging channels (discord, dingtalk, email, feishu, matrix, mochat, qq,
slack, telegram, wecom, weixin, whatsapp) are first-party plugins in
[`plugins/`](../plugins). Their classes are imported only when the gateway
starts its channels, so loading plugins never imports channel SDKs. A
first-party channel ranks like a built-in one: neither an entry point nor
another plugin can take its name.

The optional agent tools are first-party plugins too: `web` (`web_search`,
`web_fetch`) and `mira-hub` (`hub_search`, `hub_install`).
`mira plugin disable web` takes web access away from the agent and its
subagents. Subagents get only the web tools from plugins unless their role's
allowlist names others; `hub_install` and community plugin tools stay with
the main loop. Tools that are part of the agent loop itself (files, exec,
message, spawn, workflow, cron, knowledge, pubmed, community) stay in the
engine.

`entry` is `module:attr` inside the plugin directory; the directory is imported
as its own package, so relative imports work.

### Tool entries and the plugin context

A tool `entry` can name a `Tool` subclass with a no-argument constructor, a
`Tool` subclass whose `__init__` takes `ctx`, or a factory `def make(ctx) ->
Tool`. Every agent loop (and subagent) builds its own instances, so `ctx`
carries that loop's values (`mira_engine.plugins.context.PluginContext`):

| Field | |
|---|---|
| `workspace` | the loop's workspace (for an out-of-process plugin: the engine's, only if `permissions.filesystem` grants it) |
| `proxy` | the configured web proxy |
| `config` | a `ConfigView`: `ctx.config.get("channels.discord.token")`. Plugins that aren't core-bundled only see the paths listed in `permissions.secrets` (and their subtrees) |
| `data_dir` | the plugin's private directory, `<mira home>/plugin-data/<id>` |
| `host` | engine values for core-bundled plugins (e.g. `brave_api_key`); empty otherwise |

A tool that can't be built for a loop (its factory raises) is left out of that
loop and logged; the plugin's other tools still load.

## LLM providers

A plugin can add an LLM provider. `provides.providers` holds the provider's
registry metadata as data (`spec`: the fields of
`mira_engine.providers.registry.ProviderSpec`) and the plugin's hooks:

```json
"providers": [{
  "spec": { "name": "my_llm", "keywords": ["my-llm"], "display_name": "My LLM" },
  "entry": "llm:create",
  "login": "llm:login",
  "login_status": "llm:login_status",
  "models": "llm:fetch_models",
  "endpoint": "https://api.my-llm.example",
  "model_examples": ["my-llm-large"]
}]
```

| Hook | Signature | Used by |
|---|---|---|
| `entry` (required) | `create(*, spec, model, api_key, api_base, extra_headers, proxy) -> LLMProvider` | every agent turn |
| `login` | `login(print_fn, prompt_fn)`, raising on failure | `mira onboard` (OAuth providers) |
| `login_status` | `login_status() -> (logged_in, account_id)`, no network | `mira status` |
| `models` | `async fetch_models(*, model, api_base, proxy) -> ModelCache` | the model list in the desktop app, `mira models refresh` |

The provider is configured like any other, under `providers.<name>` in
config.json (`providers.myLlm.apiKey`). It's listed in the desktop app and
matched by model prefix or keyword after the core providers, whose names a
plugin can't take. Because a provider sees every prompt and holds the user's
keys, only first-party plugins and plugins the registry rated **verified**
can contribute one. OpenAI Codex and GitHub Copilot are first-party provider
plugins (`plugins/openai-codex`, `plugins/github-copilot`).

## Skills and skill packs

A plugin's `provides.skills` (`[{"id": "howto", "path": "skills/howto"}]`, each
path a directory with a `SKILL.md`) reach the agent exactly like a skill pack's:
they're listed in the desktop app's *Skills & Plugins* settings (source
`kernel`), can be switched on and off per skill, group and project, and load
while the plugin itself is enabled. `mira_engine.plugins.catalog` is the one
list of installed plugins, read from manifests alone (no plugin code), so the
skill manager and config-time code can use it.

Installing a package from the desktop app sorts it by what it contains:
skills only → a skill pack; a `plugin.json` that provides channels, tools or
hooks → `~/.mira/plugins/<id>`, **switched off** until `mira plugin enable <id>`
(its manifest can't switch itself on). The agent's `hub_install` installs
skills only and refuses packages with code. `mira plugin list` shows skill
packs next to plugins.

## What the kernel guarantees

| Failure | Effect |
|---|---|
| invalid manifest, API mismatch, import error, wrong entry type | that plugin is not loaded (`mira plugin list` shows why); everything else runs |
| a tool raises or exceeds its timeout (60 s) | the model gets a tool error |
| a hook raises or exceeds its timeout (2 s) | the call is skipped |
| 3 consecutive failures | the plugin is **tripped** and stays off until `mira plugin enable`; a core-bundled plugin is only **paused** for 5 minutes (nothing persisted) |
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

### Permissions inside the engine

A plugin that runs in process (`community`, `verified`) is checked against its
`permissions` while its code runs — tool calls, hooks, its channel (start,
send, stop and the tasks it starts), its import and construction:

| Declared | Without it, the plugin gets a `PermissionError` when it… |
|---|---|
| `network` (any host) | connects to an internet address (loopback included) or resolves a host name |
| `filesystem: workspace:write` | writes to the workspace (its own data dir, the media dir and the temp dir are always writable) |
| `filesystem: workspace:read` | reads the workspace if it lives in the Mira home |
| — | reads anything else in the Mira home (config, keys, the gateway token) |
| `shell: true` | starts a process |

The tool call fails with that error, and a plugin that keeps overstepping
trips its breaker (the trip is the engine's own write to its state file, so
it is persisted whatever the plugin may write). This is a **tripwire, not a
sandbox**: Python code in the
engine's process can get around it, which is why plugins the platform hasn't
vouched for run out of process. Declared host names are only logged, not
enforced, in process. First-party plugins aren't checked; set
`MIRA_PLUGIN_AUDIT=1` to log what they do beyond their manifests.

### Out-of-process plugins

An experimental plugin's tools are served by a host process (`python -m
mira_engine.plugins.host`, or the desktop build's own `--mira-plugin-host`
mode), sandboxed:

| | Linux (bubblewrap) | macOS (Seatbelt, `sandbox-exec`) |
|---|---|---|
| reads | system dirs and its own code, read-only; nothing else is mounted | everything outside the home directory except the Mira home and an ungranted workspace; in the home, only its interpreter, its own code and its grants |
| writes | its scratch dir (`<mira home>/plugin-data/<id>`, also its temp dir), a `workspace:write` workspace | the same |
| workspace | only with `workspace:read` / `workspace:write` | the same |
| Mira home (config, keys, gateway token) | masked | hidden |
| network | none unless `permissions.network` is non-empty | the same |
| other programs | — | refused unless `shell: true` |

Either way the host gets a minimal environment (no API keys, only its
declared secrets), a memory limit, and is killed when a call hangs; a crash
fails that call and the next call starts a fresh host. Inside the host the
permission tripwire runs as well (so `shell: false` holds on Linux too).

Without a sandbox — Windows, or Linux without working bwrap — experimental
plugins don't load unless `plugins.unsandboxed_experimental` is set; they then
still run out of process (crashes and hangs stay isolated) with only the
in-host tripwire checking their permissions.

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
