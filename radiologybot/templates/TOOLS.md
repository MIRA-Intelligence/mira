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
- For long-running scripts, use `nohup` or redirect output to a log file
- When running experiments, capture both stdout and stderr: `python script.py 2>&1 | tee log.txt`
- Check GPU availability before launching training: `python -c "import torch; print(torch.cuda.is_available())"`

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
