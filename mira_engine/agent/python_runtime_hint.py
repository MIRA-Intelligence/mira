"""Render the system-prompt hint that teaches the agent how to use the
project-local virtualenv produced by ``ExecTool``'s python runtime
manager (PR 4).

Kept in a standalone module so it can be unit-tested without spinning up
``BaseAgentLoop`` and so :mod:`research_loop` can reuse it identically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from mira_engine.config.schema import PythonRuntimeConfig


def build_python_runtime_hint(runtime: "PythonRuntimeConfig | None") -> str | None:
    """Return a prompt section describing the active Python runtime.

    Emits content only when ``manager == 'uv'``. With the default
    ``manager == 'off'`` (or a missing config) the function returns
    ``None`` so the system prompt is byte-identical to today's behaviour.

    The returned text:

    - states that exec auto-activates a project-local venv;
    - tells the agent to install with ``uv pip install`` / ``uv add``
      instead of bare ``pip``;
    - documents pinned python version and baseline packages when set;
    - warns that the first python command in a fresh project pays a
      bootstrap cost.
    """
    if runtime is None or getattr(runtime, "manager", "off") != "uv":
        return None

    lines: list[str] = [
        "## Python environment",
        "",
        "This project runs inside its own isolated Python virtualenv at "
        f"`{runtime.venv_dir}/` (managed by `uv`). The `exec` tool "
        "automatically activates it for every command, so:",
        "",
        "- Run scripts with bare `python script.py`; do not call "
        "`/usr/bin/python` or `python3` with an absolute path.",
        "- Install dependencies with `uv pip install <pkg>` or "
        "`uv add <pkg>` (which also updates `pyproject.toml` / "
        "`uv.lock` if present). Do **not** call `pip install` directly "
        "\u2014 it bypasses lockfile maintenance and may install into "
        "the wrong interpreter on some hosts.",
        "- To run something in the project's environment from outside "
        "the activated shell, use `uv run <cmd>`.",
        "- Do not create additional venvs in subdirectories. The "
        "project venv is shared across the whole project tree.",
        "- The first python-shaped command in a fresh project may "
        "take a few seconds to bootstrap dependencies; subsequent "
        "calls are instant.",
    ]
    if runtime.python_version:
        lines.append(
            f"- The interpreter is pinned to Python {runtime.python_version}."
        )
    if runtime.baseline_requirements:
        lines.append(
            "- Pre-installed baseline packages: "
            + ", ".join(f"`{p}`" for p in runtime.baseline_requirements)
            + "."
        )
    return "\n".join(lines)
