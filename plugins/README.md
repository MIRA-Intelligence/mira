# Plugins

First-party plugins shipped with mira. Each directory is one plugin: a
`plugin.json` manifest plus its code, loaded by the plugin kernel
(`mira_engine/plugins`) exactly like a plugin a user installs into
`~/.mira/plugins` — the same contract, the same guards. In a wheel and in the
desktop build this directory ships as `mira_engine/_bundled_plugins`. The
desktop build (`mira-engine.spec`) can't see a plugin's imports, so a
dependency a plugin needs there must be listed in its `hiddenimports`.

What's here: the messaging channels (`discord`, `dingtalk`, `email`, `feishu`,
`matrix`, `mochat`, `qq`, `slack`, `telegram`, `wecom`, `weixin`,
`whatsapp`), the optional agent tools (`web`: web_search/web_fetch;
`mira-hub`: hub_search/hub_install) and the OAuth LLM providers
(`openai-codex`, `github-copilot`). Tests for a plugin's code live in the engine's `tests/`, which
imports plugins as `mira_plugins.<id>.<module>` (see `tests/conftest.py`).

This directory is the community **plugin lane**: changes here are reviewed by
the community, changes to the engine are not. See `mira plugin --help` and
`mira plugin test <dir>` to check a plugin against the contract.
