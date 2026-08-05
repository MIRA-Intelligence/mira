# <img src="icon.ico" width="40" height="40" align="top"> Mira

[![Tests](https://github.com/MIRA-Intelligence/mira/actions/workflows/tests.yml/badge.svg)](https://github.com/MIRA-Intelligence/mira/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/MIRA-Intelligence/mira/graph/badge.svg)](https://codecov.io/gh/MIRA-Intelligence/mira)

An open-source, ultra-lightweight AI assistant tailored specifically for **Medical AI Research**.

Powered by an underlying micro-agent framework, Mira is designed to execute complex medical imaging pipelines, from raw DICOM data processing to deep learning tasks, traditional radiomics, and survival analysis.

## 🔬 Built-in Medical Skills

Mira comes pre-loaded with specialized medical skills:
1. **`medical-image-analysis`**: End-to-end deep learning pipeline (classification, segmentation, detection) built on MONAI and PyTorch. Features robust 5-Fold Cross-Validation and early stopping.
2. **`radiomics`**: High-dimensional radiomic feature extraction using PyRadiomics, combined with LASSO/mRMR feature selection.
3. **`survival-analysis`**: Time-to-event statistical modeling, Kaplan-Meier curves, and Cox Proportional Hazards models via lifelines.

*Mira can also be leveraged for comprehensive literature reviews and academic manuscript writing.*

## 🛡️ Core Agent Features

Mira goes beyond standard AI wrappers by implementing a robust, production-ready agent architecture:
- **Intelligent Model Routing**: Dynamically routes sub-tasks, agent reasoning, and tool calls to the most appropriate AI models based on task complexity and context, ensuring optimal performance and cost-efficiency.
- **Strict Workspace Sandboxing (Read/Write Separation)**: The agent operates within a highly secure, confined workspace directory. Built-in filesystem and shell execution guards actively block path traversals (e.g., `cd ..`, `../`) and unauthorized updates to external paths, guaranteeing the safety of the host system. Crucially, it employs a sophisticated Read/Write separation model—allowing the agent securely to read system-level built-in skills without permitting any unauthorized edits to framework source code.
- **Workspace Organization**: Desktop clients can group projects and quick chats into persistent folders. Folder names, chat metadata, and assignments are stored in the local Mira workspace.
- **Reliable Stop Controls**: `/stop` cancels the active agent task, subagents, and background shell jobs for the current session, then returns a dedicated acknowledgement to the desktop client.
- **Explicit Skill Execution**: Desktop-selected skills are loaded as mandatory instructions for the current turn. If a selected workflow cannot continue, Mira requests approval before using a fallback.

For details on long-term conversation context, see [Memory in Mira](docs/MEMORY.md).

## 🚀 Quick Start

**1. Install**
```bash
git clone https://github.com/MIRA-Intelligence/mira.git
cd mira
pip install -e .
```

**2. Configure**
Run `mira onboard` to initialize the `config.json` and your workspace (defaults to `~/.mira`).
```bash
mira onboard
```

Then, configure your model settings and API keys in `~/.mira/config.json`:
```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.mira/",
      "model": "",
      "provider": "custom",
      "maxTokens": 8192,
      "temperature": 0.6,
      "maxToolIterations": 40,
      "memoryWindow": 100,
      "reasoningEffort": null
    }
  },
  "providers": {
    "custom": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "azureOpenai": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "anthropic": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    }
  }
}
```

## 💻 CLI Commands Reference

Mira provides a comprehensive CLI for managing your sessions and configurations:

- **`mira onboard`**
  Initialize your configuration file and local workspace directory (`~/.mira` by default). This is the first command you should run after installation.

- **`mira agent`**
  Start an interactive AI chat session against the **general-purpose agent loop** (no auto-mode, no agent profiles, no task-plan contracts — closest to the upstream nanobot baseline). You can optionally pass a prompt instantly via the `-m` flag:
  ```bash
  mira agent -m "Summarise the README and list the top 3 todos."
  ```

- **`mira research`**
  Start an interactive session against the **research-flavoured agent loop** powering the desktop UI. Adds auto-mode while-loops, agent profiles (which `AGENTS_*.md` to bootstrap), automation stop policies (token / experiment budgets), and task-plan guardrails. Use this for the kind of multi-experiment workflows the desktop app drives:
  ```bash
  mira research \
    --message "I have 77 MRI Dixon cases. Please set up a 3D classification pipeline." \
    --mode auto \
    --profile research \
    --max-tokens 200000 \
    --max-experiments 8 \
    --project-dir ~/projects/dixon-mri
  ```
  Available flags:
  - `--mode / -m` — `manual` or `auto`. `auto` only triggers the auto-continue
    while-loop when running through the **web channel** (i.e. via `mira gateway`
    + the desktop UI); CLI sessions still honour the flag for cached state but
    won't drive multi-round orchestration.
  - `--profile / -p` — `default | engineer | research` (chooses
    `AGENTS.md` / `AGENTS_EG.md` / `AGENTS_RS.md`).
  - `--max-tokens` / `--max-experiments` — automation stop thresholds.
  - `--project-dir` — forwarded as `metadata.project_dir` so guardrails and
    `task_plan.json` lookups resolve correctly.

  Both `mira agent` and `mira research` are thin wrappers around the same chat
  REPL; the only difference is which loop class (`BaseAgentLoop` vs
  `ResearchAgentLoop`) drives `_process_message`. `mira gateway` keeps using
  `ResearchAgentLoop` to match the desktop UI.

  To embed Mira in Python, see the [Python SDK guide](docs/PYTHON_SDK.md).

- **`mira status`**
  Check the current status of your Mira configuration, agent defaults, and workspace environment.

- OAuth providers (e.g., `openai-codex`, `github-copilot`) are now configured directly inside `mira onboard`.

- **`mira gateway`**
  Launch the background gateway service. This enables external API endpoints and multi-channel traffic. 

### Local Engine Service CLI

For desktop/local deployment workflows, use `mira-engine`:

```bash
mira-engine install-service
mira-engine start
mira-engine status
mira-engine logs
mira-engine doctor
mira-engine doctor --export
mira-engine upgrade --package mira
mira-engine stop
mira-engine uninstall-service
```

On macOS, `install-service` registers a user LaunchAgent at:

```bash
~/Library/LaunchAgents/com.projectmira.engine.plist
```

On Linux, `install-service` registers a user systemd unit:

```bash
~/.config/systemd/user/mira-engine.service
```

On Windows, bundle builds use a WinSW-backed Windows Service. `install-service`
registers service name:

```bash
MiraEngine
```

When installing from an elevated desktop bundle installer, pass the target user
home so the service reads and writes that user's `~/.mira` data:

```bash
mira-engine install-service --home "%USERPROFILE%" --config "%USERPROFILE%\.mira\config.json"
```

Local engine logs and diagnostics:

- Logs: `~/.mira/logs/agent-service.log` (+ rotated files)
- Diagnostics bundles: `~/.mira/runtime/diagnostics/`
- Upgrade and rollback steps: [Local Engine Upgrade Runbook](docs/local-engine-upgrade-runbook.md)

## 🔗 Release Compatibility Mapping

UI ↔ Agent release compatibility is tracked in the **`mira-ui` repo** (`compatibility.json` there),
since the UI is the consumer of the agent's API and is the side that needs to declare what it works with.

The agent's own contribution to that handshake is the `api_contract` field on `GET /version`,
sourced from `_API_CONTRACT_VERSION` in `mira_engine/channels/ui.py`. Bump that constant
(and only that constant) whenever the wire format changes in a backward-incompatible way.

## 📦 Agent Release Pipeline

Tagging `v*` triggers `.github/workflows/agent-release.yml` to:

- build/test the project on Linux/macOS/Windows
- publish `mira` package artifacts (wheel/sdist)
- build standalone `mira-engine` executables with checksums

Use `.github/workflows/release-train.yml` (`workflow_dispatch`) to validate an
`agent_tag + ui_tag` pair and run smoke checks before announcing a combined release.

Release operators should also read the [deployment blueprint](DEPLOYMENT_RELEASE_BLUEPRINT.md)
and [release-day checklist](RELEASE_DAY_CHECKLIST.md).

## 🏗️ Optional Self-hosted Path

Docker-related files are in `deploy/`:

- `deploy/docker-compose.yml`
- `deploy/Dockerfile`
- `deploy/entrypoint.sh`
- `deploy/.env.example`

Compose services include:
- local build/run services: `mira-gateway`, `mira-api`, `mira-cli`
- self-hosted release services (profile `self-hosted`): `mira-engine`, `mira-ui`

Operator guide:

- `docs/self-hosted-docker.md`

## 💬 Multi-Channel Deployment (Coming Soon)
Features to deploy Mira seamlessly to platforms like Telegram, Discord, Feishu, or Slack to assist your research team in real-time are in active development.

Developers can build external integrations with the [Channel Plugin Guide](docs/CHANNEL_PLUGIN_GUIDE.md).

## 🤝 Contributing / CLA

All external contributions require acceptance of the Contributor License Agreement.
See `CLA.md` for details. By submitting a PR, you confirm acceptance of this CLA.

Contributors using the CNB mirror should follow the [GitHub and CNB synchronization guide](docs/cnb-github-sync.md).

## 🙏 Acknowledgments

The foundational CLI framework of Mira is built heavily upon the [mira](https://github.com/MIRA-Intelligence/mira). We sincerely thank the HKUDS team for their excellent open-source contribution to the community.

---
*Developed for researchers, by ECNU SKMR Lab.*
