# Task Plan — Mira 3-Stage Schema (Research → Experiment → Result)

Maintain a `task_plan.json` in your **Project Directory** (from Runtime Context).
The dashboard reads this file to display structured progress across three stages.

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
  "research": {
    "references": [
      {
        "id": "R1",
        "title": "Physics-Informed Deep Learning for qMRI",
        "authors": "Ma et al.",
        "year": "2024",
        "venue": "MRM",
        "url": "https://doi.org/...",
        "summary": "Proposes physics-driven loss for T1/T2 mapping.",
        "relevance": "Core prior-design reference for our approach"
      }
    ],
    "notes": [
      "Signal model: S(TE,TR) = M0*(1-exp(-TR/T1))*exp(-TE/T2)",
      "Existing methods assume Gaussian noise — may break for low SNR"
    ],
    "survey": "A brief literature overview paragraph..."
  },
  "experiments": [
    {
      "id": "Exp001",
      "title": "Pixel MLP baseline",
      "status": "completed",
      "question": "Is pixel-wise mapping without image priors feasible?",
      "hypothesis": "PixelMLP can achieve >0.85 SSIM on synthetic data",
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
      "id": "Exp003",
      "title": "Domain gap evaluation",
      "status": "running",
      "progress": {
        "epoch": 290,
        "total_epochs": 500,
        "current_metric": "val_ssim",
        "current_value": 0.92
      }
    },
    {
      "id": "Exp004",
      "title": "Normalization ablation",
      "status": "pending"
    }
  ],
  "knowledge": [
    "Zero-init fixes sigmoid dead zone for T2 output head (Exp005b)",
    "Forward consistency is ineffective — physics equations too imprecise (Exp008-010)"
  ],
  "result": {
    "summary": "Physics-informed U-Net achieves 0.96 SSIM on real data...",
    "output_path": "results/final_report.pdf",
    "output_type": "paper",
    "sections": [
      {
        "title": "Abstract",
        "content": "We propose PDPE-Net..."
      },
      {
        "title": "Conclusion",
        "content": "Our approach improves T2 mapping accuracy by 12%..."
      }
    ]
  }
}
```

## Top-level fields

| Field | Type | Required | Stage |
|-------|------|----------|-------|
| `title` | `string` | YES | — |
| `core_question` | `string` | YES | — |
| `status` | `string` (`in_progress` / `completed` / `failed`) | YES | — |
| `started_at` | `string` (ISO 8601) | YES | — |
| `current_experiment` | `string` (id of active experiment) | NO | Experiment |
| `research` | `object` | NO | Research |
| `experiments` | `array` | YES | Experiment |
| `knowledge` | `string[]` (accumulated discoveries) | NO | Experiment |
| `result` | `object` | NO | Result |

## Research fields

| Field | Type | Required |
|-------|------|----------|
| `references` | `array` of reference objects | NO |
| `notes` | `string[]` | NO |
| `survey` | `string` (literature overview) | NO |

### Reference object

| Field | Type |
|-------|------|
| `id` | `string` (e.g. `R1`, `R2`) |
| `title` | `string` |
| `authors` | `string` |
| `year` | `string` |
| `venue` | `string` |
| `url` | `string` |
| `summary` | `string` |
| `relevance` | `string` |

## Experiment fields

| Field | Type | Required |
|-------|------|----------|
| `id` | `string` (e.g. `Exp001`, `Exp005b`) | YES |
| `title` | `string` | YES |
| `status` | `string` (`pending` / `running` / `completed` / `failed` / `skipped`) | YES |
| `question` | `string` | NO for `pending`, YES once running/completed |
| `hypothesis` | `string` | NO for `pending`, YES once running/completed |
| `prediction` | `string` | NO for `pending`, YES once running/completed |
| `method` | `string` | NO |
| `results` | `object` (`metrics`, `findings`, `artifacts`) | NO |
| `conclusion` | `string` | NO |
| `next` | `string` | NO |
| `commit` | `string` | NO |
| `progress` | `object` (`epoch`, `total_epochs`, `current_metric`, `current_value`) | NO |
| `parent` | `string` (parent experiment id) | NO |

## Result fields

| Field | Type | Required |
|-------|------|----------|
| `summary` | `string` (final summary) | NO |
| `output_path` | `string` (file path to deliverable) | NO |
| `output_type` | `string` (`paper` / `report` / `analysis` / `code`) | NO |
| `sections` | `array` of `{title, content}` | NO |

## Rules

- Populate `research` early — add references and notes during the research phase
- After research, pre-populate `experiments` with the planned queue using
  `pending` entries so the UI can show upcoming experiments before execution
- Only **one experiment** should be `running` at a time
- Each experiment follows: question → hypothesis → prediction → experiment → analysis
- Status semantics:
  - `completed`: experiment execution finished and results were analyzed (even if
    results are poor or hypothesis is rejected)
  - `failed`: experiment procedure failed to complete due to execution/runtime
    problems
  - `skipped`: experiment intentionally not executed
- When a `pending` experiment begins, update the existing entry instead of
  appending a duplicate experiment with the same ID
- Update `current_experiment` when starting a new experiment
- Add to `knowledge[]` when you discover something broadly applicable
- Populate `result` when generating final deliverables
- The UI shows 3 clickable stages: **Research → Experiment → Result**
- If proposing a new experiment batch after prior experiments completed, keep
  old entries, append new sequential IDs, and set top-level `status` to
  `in_progress`
