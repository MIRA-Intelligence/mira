# RadiologyBot

An open-source, ultra-lightweight AI assistant tailored specifically for **Radiology AI Research**.

Powered by an underlying micro-agent framework, RadiologyBot is designed to execute complex medical imaging pipelines, from raw DICOM data processing to deep learning tasks, traditional radiomics, and survival analysis.

## 🔬 Built-in Medical Skills

RadiologyBot comes pre-loaded with specialized medical skills:
1. **`medical-image-dl-pipeline`**: End-to-end deep learning pipeline (classification, segmentation, detection) built on MONAI and PyTorch. Features robust 5-Fold Cross-Validation and early stopping.
2. **`radiomics`**: High-dimensional radiomic feature extraction using PyRadiomics, combined with LASSO/mRMR feature selection.
3. **`survival-analysis`**: Time-to-event statistical modeling, Kaplan-Meier curves, and Cox Proportional Hazards models via lifelines.

*RadiologyBot can also be leveraged for comprehensive literature reviews and academic manuscript writing.*

## 🚀 Quick Start

**1. Install**
```bash
git clone https://github.com/ldxFAIRYTAIL/RadiologyBot.git
cd RadiologyBot
pip install -e .
```

**2. Configure**
Run `radiologybot onboard` to initialize the `config.json` and your workspace (defaults to `~/.radiologybot`).
```bash
radiologybot onboard
```

Then, configure your model settings and API keys in `~/.radiologybot/config.json`:
```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.radiologybot/",
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

RadiologyBot provides a comprehensive CLI for managing your sessions and configurations:

- **`radiologybot onboard`**
  Initialize your configuration file and local workspace directory (`~/.radiologybot` by default). This is the first command you should run after installation.

- **`radiologybot agent`**
  Start an interactive AI chat session directly in your terminal. You can optionally pass a prompt instantly via the `-m` flag:
  ```bash
  radiologybot agent -m "I have 77 MRI Dixon cases. Please set up a 3D classification pipeline to predict expiration vs. inspiration."
  ```

- **`radiologybot status`**
  Check the current status of your RadiologyBot configuration, agent defaults, and workspace environment.

- **`radiologybot provider-login <provider>`**
  Authenticate interactively via OAuth for supported models and providers (e.g., `openai-codex`, `github-copilot`).

- **`radiologybot gateway`**
  Launch the background gateway service. This enables external API endpoints and multi-channel traffic. 

## 💬 Multi-Channel Deployment (Coming Soon)
Features to deploy RadiologyBot seamlessly to platforms like Telegram, Discord, Feishu, or Slack to assist your research team in real-time are in active development.

## 🙏 Acknowledgments

The foundational CLI framework of RadiologyBot is built heavily upon the [nanobot](https://github.com/HKUDS/nanobot). We sincerely thank the HKUDS team for their excellent open-source contribution to the community.

---
*Developed for researchers, by researchers.*
