---
description: "Maintain a structured task_plan.json for tracking multi-step research progress"
metadata: '{"medpilot": {"requires": {}}}'
---

# Task Plan — Structured Progress Tracking

When working on multi-step research tasks, maintain a `task_plan.json` file so that
external dashboards or logs can display structured progress.

## Lifecycle

1. **Create** `task_plan.json` when you begin work
2. **Update** it each time a step or phase changes status
3. **Mark completed** when the overall task finishes

Write the **full** JSON every time (not a patch).

## Schema

```json
{
  "title": "Project title",
  "pipeline_stage": "planning",
  "status": "in_progress",
  "started_at": "2026-03-24T12:00:00Z",
  "steps": [
    {
      "number": 1,
      "title": "Step description",
      "status": "completed",
      "results": {
        "findings": "Summary of findings.",
        "artifacts": ["path/to/artifact.json"]
      }
    },
    {
      "number": 2,
      "title": "Another step",
      "status": "running"
    }
  ]
}
```

## Required top-level fields

| Field | Type | Values | Required |
|-------|------|--------|----------|
| `title` | `string` | — | YES |
| `pipeline_stage` | `string` | free-form stage label | YES |
| `status` | `string` | `in_progress` · `completed` · `failed` | YES |
| `started_at` | `string` | ISO 8601 datetime | YES |
| `steps` | `array` | Array of step objects | YES |

## Step fields

| Field | Type | Required |
|-------|------|----------|
| `number` | `integer` | YES |
| `title` | `string` | YES |
| `status` | `string` (`pending`/`running`/`completed`/`failed`) | YES |
| `phases` | `array` of `{label, status}` | NO |
| `results` | `object` with `metrics`, `findings`, `artifacts` | NO |

## Rules

- Only **one step** should be `running` at a time
- Always write the complete JSON — never a partial patch
