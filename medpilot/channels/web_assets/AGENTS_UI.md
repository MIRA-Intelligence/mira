# MedPilot — Web Dashboard Instructions

These instructions apply **only** when your Runtime Context shows `Channel: web`.
Runtime Context also includes `Run Mode` (`manual` or `auto`) provided by the UI.

## Project Directory

- Your Runtime Context includes a **Project Directory** — an absolute path
  like `/Users/x/.medpilot/workspace/PRJ-0001`.
- All project files — including `task_plan.json` — MUST be written under this directory.
  Example: `write_file("/Users/x/.medpilot/workspace/PRJ-0001/task_plan.json", ...)`
- Create the project directory first if it does not exist.

## task_plan.json

Maintain a `task_plan.json` file in your Project Directory so the dashboard can
display structured progress. The UI has **three stages** that the user can
switch between:

1. **Research** — literature references, survey notes, background reading
2. **Experiment** — numbered experiments (Exp001, Exp002, ...) following the
   scientific method
3. **Result** — final deliverables (paper, report, analysis, code)

Populate the `research` section early when you are surveying the literature.
After research, initialize the `experiments` array with the planned experiment
sequence so the dashboard can show the queue before execution begins. Fill in
`result` when generating final output.

## Research Phase

When starting a new project, begin with background research:
- If `Project Directory/references/` contains uploaded materials, read those local files first and ground your initial survey in them before broad external search.
- Search for relevant literature and add references to `task_plan.json` → `research.references`
- Write a brief survey overview in `research.survey`
- Note key observations and domain-specific facts in `research.notes`
- Before stopping, write the planned experiment queue into `task_plan.json` → `experiments`
  using `pending` entries (`Exp001`, `Exp002`, ...). Include at least `id`,
  `title`, and `status`, and add `question` / `hypothesis` / `prediction` early
  if you already know them.
- In `manual` mode: after research, STOP and report findings before moving to experiments.
- In `auto` mode: continue directly into the next pending experiment without waiting.

## Experiment-by-Experiment Execution — MANDATORY

You MUST work **one experiment at a time**. Each experiment follows:

```
Question → Hypothesis → Prediction → Experiment → Analysis → Conclusion
```

**CRITICAL RULE (mode-dependent):**
- In `manual` mode: after completing each experiment (or after it fails), you MUST
  STOP and return a summary. Do NOT proceed until the user explicitly says
  "continue" or gives further instructions.
- In `auto` mode: continue to the next pending experiment automatically. Only stop
  early when user input is strictly required, the project is blocked by an error,
  or there are no pending/running experiments left.
- In `auto` mode: in a single assistant turn, you may transition AT MOST ONE
  experiment to a terminal status (`completed`/`failed`/`skipped`). You may
  create or queue many `pending` experiments, but finish only one per turn.

### Workflow for each experiment

1. **Design**: Formulate a clear question, hypothesis, and prediction.
   If the experiment is already listed as `pending`, update that entry in
   `task_plan.json` and set it to `running`. Otherwise create it with status
   `running`.

2. **Execute**: Implement and run the experiment. Update `progress` in
   `task_plan.json` if applicable (epoch counts, intermediate metrics).

3. **Analyze**: Evaluate results against predictions. Fill in `results`,
   `conclusion`, and `next` in `task_plan.json`.
   - Use `completed` for any experiment that finished execution and produced an
     analyzable outcome, even if the result is poor or the hypothesis is
     rejected.
   - Use `failed` only when the experiment procedure itself fails (for example:
     runtime error, corrupted input, environment crash, or unrecoverable tool
     failure) and the run did not complete normally.
   - Use `skipped` for experiments intentionally skipped (for example: replaced
     by a better plan, deemed unnecessary, blocked by scope/time, or user
     request).

4. **Report**: Return a concise summary to the user:
   - What was the question/hypothesis?
   - What happened? (key metrics)
   - What does this mean? (conclusion)
   - What should we do next? (proposed next experiment)
   - In `manual` mode: then **STOP** and wait for user confirmation.
   - In `auto` mode: do **NOT** stop here; proceed to the next pending experiment automatically.

5. **Wait/Continue**:
   - `manual`: wait for user confirmation.
   - `auto`: continue automatically to the next pending experiment.

### Example response at end of an experiment

> **Exp005 completed: Domain Gap Evaluation**
> - **Question**: How large is the synthetic-to-real domain gap?
> - **Results**: PixelMLP real SSIM=0.739 (gap=-20%), PiUNet real SSIM=0.756 (gap=-20%)
> - **Conclusion**: Both models show ~20% domain gap. PiUNet slightly better on real data.
> - **Proposed next**: Exp006 — Test normalization strategies to reduce domain gap.

### Knowledge accumulation

When you discover something important that applies beyond the current experiment,
add it to the `knowledge` array in `task_plan.json`. Examples:
- "Zero-init fixes sigmoid dead zone for T2 output head"
- "Per-sample normalization causes 55% of domain gap"
- "Forward consistency is ineffective when physics equations are imprecise"

### Experiment naming

- Use sequential IDs: `Exp001`, `Exp002`, `Exp003`, ...
- For variants/branches: `Exp005b`, `Exp005c` (set `parent: "Exp005"`)
- Git commits: `ExpNNN: brief description`
- The `experiments` array should contain the full planned queue, not only
  experiments that have already started.
- Allowed experiment statuses: `pending`, `running`, `completed`, `failed`,
  `skipped`.

### Re-planning after a completed batch

When the user asks to re-plan based on completed experiments and current
`knowledge`:
- Read all existing experiment outcomes from `task_plan.json` first.
- Keep historical experiments (especially completed/failed ones) in the array;
  do not drop prior records.
- Append a new batch with next sequential IDs (`Exp00X` ...), usually as
  `pending`, and set `current_experiment` to the first new candidate when
  appropriate.
- Set project `status` to `in_progress` when new experiments are proposed.
- Write the full updated `task_plan.json` before sending the final reply so the
  dashboard can immediately render the new queue.

## Result Phase

When the user requests a final deliverable, populate the `result` section in
`task_plan.json`:
- `summary`: a concise summary of all findings
- `output_path`: the file path to the generated deliverable (relative to project dir)
- `output_type`: one of `paper`, `report`, `analysis`, `code`
- `sections`: structured content sections (title + content pairs)

### Additional rules

- This 3-stage protocol is **only** needed on the web channel.
- Always write the **full** `task_plan.json` (not a patch).
- Only one experiment should be `running` at a time.
