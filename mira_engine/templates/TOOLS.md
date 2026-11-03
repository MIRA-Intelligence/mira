# Tool Usage Notes

Tool signatures come from function calling; these are the non-obvious constraints.

- `exec` blocks dangerous commands, truncates output at 10,000 characters, and stops foreground commands at the configured timeout (hard cap 10 minutes). Run anything that may take minutes (training, large preprocessing, long simulations) with `background=true` and follow it with `bg`.
- Experiments: set random seeds (`PYTHONHASHSEED=0` plus code-level seeds), capture stdout and stderr in a log (`python script.py 2>&1 | tee log.txt`), and check GPU availability before training.
- Read a file before modifying it, and re-read a script after writing it before running it. Keep experiment scripts self-contained and runnable on their own.
- Git: check `git status` and `git diff --stat` before committing.
