# SciAgentUI — Web Dashboard Instructions

These instructions apply **only** when your Runtime Context shows `Channel: web`.

## Project Directory

- Your Runtime Context includes a **Project Directory** — an absolute path
  like `/Users/x/.radiologybot/workspace/PRJ-0001`.
- All project files — including `task_plan.json` — MUST be written under this directory.
  Example: `write_file("/Users/x/.radiologybot/workspace/PRJ-0001/task_plan.json", ...)`
- Create the project directory first if it does not exist.

## task_plan.json

Maintain a `task_plan.json` file in your Project Directory so the dashboard can
display structured experiment progress. The UI is **experiment-centric** — it
tracks a sequence of numbered experiments (Exp001, Exp002, ...), each following
the scientific method.

## Experiment-by-Experiment Execution — MANDATORY

You MUST work **one experiment at a time**. Each experiment follows:

```
Question → Hypothesis → Prediction → Experiment → Analysis → Conclusion
```

**CRITICAL RULE: After completing each experiment (or after it fails), you MUST
STOP and return a summary. Do NOT proceed to the next experiment until the user
explicitly says "continue" or gives further instructions.**

### Workflow for each experiment

1. **Design**: Formulate a clear question, hypothesis, and prediction.
   Create/update `task_plan.json` with the new experiment entry (status: `running`).

2. **Execute**: Implement and run the experiment. Update `progress` in
   `task_plan.json` if applicable (epoch counts, intermediate metrics).

3. **Analyze**: Evaluate results against predictions. Fill in `results`,
   `conclusion`, and `next` in `task_plan.json`. Set status to `completed`
   or `failed`.

4. **Report**: Return a concise summary to the user:
   - What was the question/hypothesis?
   - What happened? (key metrics)
   - What does this mean? (conclusion)
   - What should we do next? (proposed next experiment)
   Then **STOP** — do not start the next experiment.

5. **Wait**: The user (or the UI in auto-mode) will tell you when to continue.

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

### Additional rules

- This experiment-by-experiment protocol is **only** needed on the web channel.
- Always write the **full** `task_plan.json` (not a patch).
- Only one experiment should be `running` at a time.
