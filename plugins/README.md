# Plugins

First-party plugins shipped with mira. Each directory is one plugin: a
`plugin.json` manifest plus its code, loaded by the plugin kernel
(`mira_engine/plugins`) exactly like a plugin a user installs into
`~/.mira/plugins` — the same contract, the same guards. In a wheel this
directory ships as `mira_engine/_bundled_plugins`.

This directory is the community **plugin lane**: changes here are reviewed by
the community, changes to the engine are not. See `mira plugin --help` and
`mira plugin test <dir>` to check a plugin against the contract.
