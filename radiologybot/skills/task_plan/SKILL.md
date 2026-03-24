---
description: "Maintain a structured task_plan.json for SciAgentUI dashboard progress tracking"
metadata: '{"radiologybot": {"requires": {}}}'
---

# Task Plan — Structured Progress Tracking for SciAgentUI

When working through the **web** channel (SciAgentUI dashboard), maintain a `task_plan.json`
file in your **Project Directory** (from Runtime Context). The dashboard reads this file to
render structured progress in the task detail panel.

## Lifecycle

1. **Create** `task_plan.json` when you formulate a plan for a new task
2. **Update** it each time a step or phase changes status
3. **Add results** to a step when it completes (metrics, findings, artifacts)
4. **Mark completed** when the overall task finishes

Write the file to: `write_file("projects/{Chat ID}/task_plan.json", ...)`
Always write the **full** JSON (not a patch).

## Schema — EXACT format required

The UI parses this JSON strictly. Use ONLY these fields — do NOT add custom fields.

```json
{
  "title": "Project or task title",
  "pipeline_stage": "ideation",
  "status": "in_progress",
  "started_at": "2026-03-22T12:00:00Z",
  "steps": [
    {
      "number": 1,
      "title": "Concise step description",
      "status": "completed",
      "results": {
        "metrics": { "accuracy": 0.92, "loss": 0.31, "gpu_hours": 2.4 },
        "findings": "Summary of what was learned from this step.",
        "artifacts": ["plots/loss_curve.png", "results/metrics.csv"]
      },
      "phases": [
        { "label": "Sub-task A", "status": "completed" },
        { "label": "Sub-task B", "status": "completed" }
      ]
    },
    {
      "number": 2,
      "title": "Current step",
      "status": "running"
    },
    {
      "number": 3,
      "title": "Future step",
      "status": "pending"
    }
  ]
}
```

## Required top-level fields

| Field | Type | Values | Required |
|-------|------|--------|----------|
| `title` | `string` | — | YES |
| `pipeline_stage` | `string` | `ideation` · `planning` · `experiment` · `writing` | YES |
| `status` | `string` | `in_progress` · `completed` · `failed` | YES |
| `started_at` | `string` | ISO 8601 datetime | YES |
| `steps` | `array` | Array of step objects | YES |

## Required step fields

| Field | Type | Values | Required |
|-------|------|--------|----------|
| `number` | `integer` | Sequential from 1 | YES |
| `title` | `string` | — | YES |
| `status` | `string` | `pending` · `running` · `completed` · `failed` | YES |
| `phases` | `array` | Phase objects (optional) | NO |
| `results` | `object` | Results object (optional) | NO |

## Results object (per step)

| Field | Type | Meaning |
|-------|------|---------|
| `metrics` | `object` | Key-value pairs of numeric or string metrics |
| `findings` | `string` | Brief summary — displayed as text in the UI |
| `artifacts` | `string[]` | Relative paths to output files — shown as links |

## Rules

- Only **one step** should be `running` at a time
- Steps MUST have `number` (integer), `title`, and `status`
- Do NOT add extra fields like `id`, `description`, `dependencies`, or `outputs`
- Phases are optional — add only when a step has meaningful sub-tasks
- **Add `results` when a step completes** — include key metrics, a brief finding, and artifact paths
- You can add partial results to a `running` step (e.g., intermediate metrics)
- Update `pipeline_stage` as the work progresses through research phases:
  - `ideation` — literature review, brainstorming, hypothesis formation
  - `planning` — experimental design, protocol setup
  - `experiment` — running experiments, collecting data
  - `writing` — analysis, paper writing, documentation
