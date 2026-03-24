# SciAgentUI — Web Dashboard Instructions

These instructions apply **only** when your Runtime Context shows `Channel: web`.

## Project Directory

- Your Runtime Context includes a **Project Directory** — an absolute path
  like `/Users/x/.sciagent/workspace/PRJ-0001`.
- All project files — including `task_plan.json` — MUST be written under this directory.
  Example: `write_file("/Users/x/.sciagent/workspace/PRJ-0001/task_plan.json", ...)`
- Create the project directory first if it does not exist.

## task_plan.json

Maintain a `task_plan.json` file in your Project Directory so the dashboard can
display structured progress. Read the schema from `skills/task_plan/SKILL.md` for
the base format. The UI extends it with:

- A `stage` field on every step (one of: `research`, `planning`, `experiment`, `writing`)
- A top-level `stage_data` object for stage-specific artifacts (see SKILL_UI below)

## Phase-by-Phase Execution — MANDATORY

You MUST work **one pipeline phase at a time**. The 4-stage pipeline is:

  `research → planning → experiment → writing`

**CRITICAL RULE: After completing each phase, you MUST STOP and return a summary
to the user. Do NOT proceed to the next phase until the user explicitly says
"continue" or gives further instructions.**

The workflow for each phase:

1. **Start the phase**: Update `task_plan.json` with `pipeline_stage` set to the current phase.
2. **Do the work for THIS phase ONLY**:
   - `research`: Literature search, gap analysis, hypothesis formation. Populate `stage_data.research`.
   - `planning`: Create a detailed execution plan with concrete steps.
   - `experiment`: Execute ONE experiment step, record metrics and results.
   - `writing`: Draft ONE section of the output document.
3. **Update `task_plan.json`** with results, findings, and artifacts for completed steps.
4. **STOP and report**: Return a concise summary of what was accomplished in this phase.
   Include key findings, metrics, or decisions. End your response — do NOT make
   further tool calls or start the next phase.
5. **Wait**: The user (or the UI in auto-mode) will tell you when to continue.

Example response at end of research phase:

> **Research phase complete.**
> - Found 5 relevant papers on chest X-ray classification
> - Key finding: DenseNet-121 achieves radiologist-level AUC
> - Identified gap: limited multi-label classification studies
> - Ready to proceed to **Planning** phase.

## What counts as "one phase"

- **research**: All literature search and analysis for this project. Stop when you have enough background.
- **planning**: The complete experimental design. Stop when the plan is written.
- **experiment**: ONE experiment step (e.g., "run baseline model"). Stop after that step completes and results are recorded. The user will tell you to continue to the next experiment step.
- **writing**: ONE section of the document. Stop after that section is drafted.
