# Project Guidelines

## Code Style

- Target Python 3.11+ and match the existing style: typed functions, short docstrings, `Path` over string paths when working with files, and modern unions like `str | None`.
- Keep changes small and local. This repo favors direct, readable implementations over extra abstraction.
- Follow the Ruff configuration in `pyproject.toml`: line length 100, sorted imports, and no reformatting unrelated code.
- Keep CLI-facing output and logging consistent with the existing Typer and Rich patterns in `mira/cli/commands.py`.

## Architecture

- Treat `mira/cli/commands.py` as the public entrypoint. CLI behavior belongs there, not in lower-level modules.
- Keep orchestration logic in `mira/agent/loop.py`. New agent capabilities should usually be implemented as tools under `mira/agent/tools/` and registered through `AgentLoop._register_default_tools()`.
- Keep configuration definitions centralized in `mira/config/schema.py` and related config modules. Preserve both camelCase and snake_case compatibility when extending config models.
- Channels under `mira/channels/` are adapters around a shared message bus. Cross-channel coordination belongs in `mira/channels/manager.py`, not inside individual channels.
- Skills and templates under `mira/skills/` and `mira/templates/` are packaged assets, not incidental docs. Preserve their structure and update build includes if you add new packaged asset types.

## Build and Test

- Install for development with `pip install -e .`. If you need lint/test tools, prefer `pip install -e ".[dev]"`.
- Common manual checks:
  - `ruff check .`
  - `pytest`
  - `python -m mira --help`
  - `mira onboard`
- `pyproject.toml` configures `pytest` to look for `tests/`, but this workspace currently has no `tests/` directory. Do not claim tests passed unless you actually added tests or ran a targeted test path that exists.

## Conventions

- Runtime state is workspace-centric but usually lives outside the repo in `~/.mira`. `mira onboard` creates that workspace and syncs bundled templates into it.
- Empty `allow_from` lists are not permissive defaults. `mira/channels/manager.py` treats `allow_from = []` as a misconfiguration that denies all access.
- Provider and channel imports are intentionally lazy in several paths. Preserve that pattern when adding optional integrations so missing dependencies fail gracefully.
- When changing tool behavior, validate the impact on both the agent loop and subagent flow rather than patching a single call site.