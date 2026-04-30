# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## exec — Scientific Computing Best Practices

- Always set random seeds before running experiments: `PYTHONHASHSEED=0` + code-level seeds
- For any command that may take longer than a few minutes (model training, large
  preprocessing, long simulations), use `exec(command=..., background=true)`
  instead of foreground `exec`. Foreground `exec` is hard-capped at 10 minutes
  wall-clock; background jobs have no such cap.
- When running experiments synchronously, capture both stdout and stderr:
  `python script.py 2>&1 | tee log.txt`
- Check GPU availability before launching training:
  `python -c "import torch; print(torch.cuda.is_available())"`

## exec — Background Jobs (long-running tasks)

Use `exec(command=..., background=true, description="...")` for anything that
might exceed the foreground 10-minute timeout. The call returns immediately
with a `job_id` (e.g. `bg-1a2b3c4d`); stdout/stderr stream to
`<workspace>/.mira/jobs/<job_id>/{stdout.log, stderr.log}`.

Then drive it with the `bg` tool:

- `bg(action="list")` — see all active and recently-finished jobs.
- `bg(action="status", job_id=...)` — single-job metadata (pid, runtime, exit code).
- `bg(action="tail", job_id=..., tail_lines=N)` — read the last N lines of stdout/stderr.
- `bg(action="wait", job_id=..., timeout=N)` — block up to N seconds (1-600);
  returns "still running" if the job hasn't finished yet, in which case call
  `wait` again or use `tail` to peek.
- `bg(action="kill", job_id=...)` — terminate a runaway job (SIGTERM then SIGKILL).

Typical pattern for a 30-minute training run:

```
exec(command="python train.py --epochs 100", background=true, description="train resnet")
# → "Started background job bg-1a2b3c4d (pid=12345). Logs: ..."
bg(action="wait", job_id="bg-1a2b3c4d", timeout=300)   # poll every 5 min
bg(action="tail", job_id="bg-1a2b3c4d", tail_lines=50) # check progress
bg(action="wait", job_id="bg-1a2b3c4d", timeout=600)   # keep waiting
# … until status reports exited
```

## exec — Git Operations

- Always `git status` before committing to verify what's staged
- Use `git diff --stat` to review changes before commit
- Commit format: `git commit -m "ExpNNN: description"`
- After commit, record the hash: `git rev-parse --short HEAD`

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## read_file / write_file / edit_file — Research Files

- Before modifying any experiment script, always read it first
- After writing a script, re-read to verify correctness before execution
- When updating MEMORY.md, preserve existing entries — append or edit, don't overwrite
- Experiment scripts should be self-contained and runnable independently
