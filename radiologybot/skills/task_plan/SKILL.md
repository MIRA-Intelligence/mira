---
description: "Maintain a structured task_plan.json for SciAgentUI dashboard progress tracking"
metadata: '{"radiologybot": {"requires": {}}}'
---

# Task Plan — Structured Progress Tracking for SciAgentUI

When working through the **web** channel (SciAgentUI dashboard), maintain a `task_plan.json`
file in the workspace root. The dashboard reads this file to render structured progress
in the task detail panel.

## Lifecycle

1. **Create** `task_plan.json` when you formulate a plan for a new task
2. **Update** it each time a step or phase changes status
3. **Add results** to a step when it completes (metrics, findings, artifacts)
4. **Mark completed** when the overall task finishes

Use `write_file("task_plan.json", ...)` — always write the **full** JSON (not a patch).

## Schema

```json
{
  "title": "Project or task title",
  "pipeline_stage": "experiment",
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
      "status": "running",
      "results": {
        "metrics": { "steps_done": "7/10", "wall_time": "5h 50m" },
        "findings": "Preliminary observation so far..."
      }
    },
    {
      "number": 3,
      "title": "Future step",
      "status": "pending"
    }
  ]
}
```

## Field reference

| Field | Values | Meaning |
|-------|--------|---------|
| `pipeline_stage` | `ideation` · `planning` · `experiment` · `writing` | Current high-level research phase |
| top-level `status` | `in_progress` · `completed` · `failed` | Overall task status |
| step `status` | `pending` · `running` · `completed` · `failed` | Per-step status |
| phase `status` | `pending` · `running` · `completed` | Per-phase status |

### Results object (per step)

| Field | Type | Meaning |
|-------|------|---------|
| `metrics` | `object` | Key-value pairs of numeric or string metrics (accuracy, loss, wall_time, etc.) |
| `findings` | `string` | Brief summary of what was learned — displayed as text in the UI |
| `artifacts` | `string[]` | Relative paths to output files (plots, CSVs, logs) — shown as links in the UI |

## Rules

- Only **one step** should be `running` at a time
- Steps are numbered sequentially starting from 1
- Phases are optional — add them only when a step has meaningful sub-tasks
- **Add `results` when a step completes** — include key metrics, a brief finding, and paths to artifacts
- You can also add partial results to a `running` step (e.g., intermediate metrics)
- Update `pipeline_stage` as the work progresses through research phases:
  - `ideation` — literature review, brainstorming, hypothesis formation
  - `planning` — experimental design, protocol setup
  - `experiment` — running experiments, collecting data
  - `writing` — analysis, paper writing, documentation
