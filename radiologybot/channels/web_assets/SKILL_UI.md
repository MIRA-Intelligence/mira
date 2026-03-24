# Task Plan — SciAgentUI Experiment-Centric Schema

Maintain a `task_plan.json` in your **Project Directory** (from Runtime Context).
The dashboard reads this file to display structured experiment progress.

Write the file using:
```
write_file("{Project Directory}/task_plan.json", ...)
```

Always write the **full** JSON (not a patch).

## Schema

```json
{
  "title": "T2mapping PDPE-Net",
  "core_question": "How to design physics priors to improve qMRI generalization?",
  "status": "in_progress",
  "started_at": "2026-03-24T12:00:00Z",
  "current_experiment": "Exp003",
  "experiments": [
    {
      "id": "Exp001",
      "title": "Pixel MLP baseline",
      "status": "completed",
      "question": "Is pixel-wise mapping without image priors feasible?",
      "hypothesis": "PixelMLP with parameter embedding can achieve >0.85 SSIM on synthetic data",
      "prediction": "SSIM > 0.85 for T1/T2/PD maps",
      "method": "Train PixelMLP (52K params) on 20 subjects, 500 epochs",
      "results": {
        "metrics": { "mean_ssim": 0.928, "t1_ssim": 0.956, "t2_ssim": 0.872 },
        "findings": "Baseline works. T2 is hardest (0.872 vs T1 0.956).",
        "artifacts": ["experiments/outputs/exp001/results.json"]
      },
      "conclusion": "Pixel-wise mapping is viable. T2 learning is the bottleneck.",
      "next": "Compare with PiUNet to quantify spatial prior contribution",
      "commit": "055b86e"
    },
    {
      "id": "Exp002",
      "title": "PiUNet baseline with spatial priors",
      "status": "completed",
      "question": "How much do spatial priors (U-Net) improve over pixel-wise?",
      "hypothesis": "PiUNet will outperform PixelMLP especially on T2",
      "prediction": "T2 SSIM > 0.90, mean SSIM > 0.94",
      "results": {
        "metrics": { "mean_ssim": 0.953, "t2_ssim": 0.924 }
      },
      "conclusion": "Spatial priors help T2 most (+5.2%).",
      "commit": "abc1234"
    },
    {
      "id": "Exp003",
      "title": "Domain gap evaluation",
      "status": "running",
      "question": "How large is the synthetic-to-real domain gap?",
      "hypothesis": "Domain gap will be larger for pixel-wise model",
      "prediction": "Real SSIM drops >15% from synthetic SSIM",
      "progress": {
        "epoch": 290,
        "total_epochs": 500,
        "current_metric": "val_ssim",
        "current_value": 0.92
      }
    }
  ],
  "knowledge": [
    "Zero-init fixes sigmoid dead zone for T2 output head (Exp005b)",
    "Forward consistency is ineffective — physics equations too imprecise (Exp008-010)",
    "Per-sample normalization causes 55% of domain gap (Exp015)"
  ]
}
```

## Required top-level fields

| Field | Type | Required |
|-------|------|----------|
| `title` | `string` | YES |
| `core_question` | `string` | YES |
| `status` | `string` (`in_progress` / `completed` / `failed`) | YES |
| `started_at` | `string` (ISO 8601) | YES |
| `current_experiment` | `string` (id of the active experiment) | YES |
| `experiments` | `array` | YES |
| `knowledge` | `string[]` (accumulated discoveries) | NO |

## Experiment fields

| Field | Type | Required |
|-------|------|----------|
| `id` | `string` (e.g. `Exp001`, `Exp005b`) | YES |
| `title` | `string` | YES |
| `status` | `string` (`pending` / `running` / `completed` / `failed`) | YES |
| `question` | `string` | YES |
| `hypothesis` | `string` | YES |
| `prediction` | `string` | YES |
| `method` | `string` | NO |
| `results` | `object` (`metrics`, `findings`, `artifacts`) | NO |
| `conclusion` | `string` | NO |
| `next` | `string` (what question this raises) | NO |
| `commit` | `string` (git commit hash) | NO |
| `progress` | `object` (for running experiments) | NO |
| `parent` | `string` (parent experiment id, for branches) | NO |

## Progress fields (for running experiments)

| Field | Type |
|-------|------|
| `epoch` | `integer` |
| `total_epochs` | `integer` |
| `current_metric` | `string` |
| `current_value` | `number` |

## Rules

- Only **one experiment** should be `running` at a time
- Each experiment follows the scientific method: question → hypothesis → prediction → experiment → analysis
- Update `current_experiment` when starting a new experiment
- Add to `knowledge[]` when you discover something important that applies across experiments
- Add `results` to an experiment when it completes
- Include `conclusion` and `next` to explain what was learned and what comes next
- Use `parent` to indicate branching (e.g., `Exp005b` has `parent: "Exp005"`)
