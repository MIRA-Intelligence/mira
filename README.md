# <img src="icon.ico" width="40" height="40" align="top"> MedPilot

An open-source, ultra-lightweight AI assistant tailored specifically for **Medical AI Research**.

Powered by an underlying micro-agent framework, MedPilot is designed to execute complex medical imaging pipelines, from raw DICOM data processing to deep learning tasks, traditional radiomics, and survival analysis.

## 🔬 Built-in Medical Skills

MedPilot comes pre-loaded with specialized medical skills:
1. **`medical-image-dl-pipeline`**: End-to-end deep learning pipeline (classification, segmentation, detection) built on MONAI and PyTorch. Features robust 5-Fold Cross-Validation and early stopping.
2. **`radiomics`**: High-dimensional radiomic feature extraction using PyRadiomics, combined with LASSO/mRMR feature selection.
3. **`survival-analysis`**: Time-to-event statistical modeling, Kaplan-Meier curves, and Cox Proportional Hazards models via lifelines.

*MedPilot can also be leveraged for comprehensive literature reviews and academic manuscript writing.*

## 🛡️ Core Agent Features

MedPilot goes beyond standard AI wrappers by implementing a robust, production-ready agent architecture:
- **Intelligent Model Routing**: Dynamically routes sub-tasks, agent reasoning, and tool calls to the most appropriate AI models based on task complexity and context, ensuring optimal performance and cost-efficiency.
- **Strict Workspace Sandboxing (Read/Write Separation)**: The agent operates within a highly secure, confined workspace directory. Built-in filesystem and shell execution guards actively block path traversals (e.g., `cd ..`, `../`) and unauthorized updates to external paths, guaranteeing the safety of the host system. Crucially, it employs a sophisticated Read/Write separation model—allowing the agent securely to read system-level built-in skills without permitting any unauthorized edits to framework source code.

## 🚀 Quick Start

**1. Install**
```bash
git clone https://github.com/Project-MedPilot/MedPilot.git
cd MedPilot
pip install -e .
```

**2. Configure**
Run `medpilot onboard` to initialize the `config.json` and your workspace (defaults to `~/.medpilot`).
```bash
medpilot onboard
```

Then, configure your model settings and API keys in `~/.medpilot/config.json`:
```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.medpilot/",
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

MedPilot provides a comprehensive CLI for managing your sessions and configurations:

- **`medpilot onboard`**
  Initialize your configuration file and local workspace directory (`~/.medpilot` by default). This is the first command you should run after installation.

- **`medpilot agent`**
  Start an interactive AI chat session directly in your terminal. You can optionally pass a prompt instantly via the `-m` flag:
  ```bash
  medpilot agent -m "I have 77 MRI Dixon cases. Please set up a 3D classification pipeline to predict expiration vs. inspiration."
  ```

- **`medpilot status`**
  Check the current status of your MedPilot configuration, agent defaults, and workspace environment.

- **`medpilot provider-login <provider>`**
  Authenticate interactively via OAuth for supported models and providers (e.g., `openai-codex`, `github-copilot`).

- **`medpilot gateway`**
  Launch the background gateway service. This enables external API endpoints and multi-channel traffic. 

## 💬 Multi-Channel Deployment (Coming Soon)
Features to deploy MedPilot seamlessly to platforms like Telegram, Discord, Feishu, or Slack to assist your research team in real-time are in active development.

## 🙏 Acknowledgments

The foundational CLI framework of MedPilot is built heavily upon the [nanobot](https://github.com/HKUDS/nanobot). We sincerely thank the HKUDS team for their excellent open-source contribution to the community.

---
*Developed for researchers, by researchers.*
